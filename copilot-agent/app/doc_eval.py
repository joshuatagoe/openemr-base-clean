"""The Week 2 tier of the golden set: real documents, real recorded model output.

The 24 Week 1 cases carry scripted model output and contain no document, so they
could not see a Week 2 regression - proven on 2026-09-23 when reporting our own
computed comparison as a lab-printed flag left the gate green.

These cases fix that. Each one is a synthetic lab PDF plus the REAL model's
response to it, recorded once (``scripts/record_evals.py``) and replayed here
for free. Replay is keyed by the prompt, the model and the document, so:

  * change the extraction prompt  -> the recording is stale -> schema_valid
    fails -> the gate fails until you re-record and look at what moved;
  * change the extraction code    -> the replayed output is scored against the
    expected answers, so a behavioural regression fails a rubric.

Scored with the same five categories as Week 1, so they share one baseline and
one set of floors. Three cases come from the Week 2 starter working set (S01,
S03, S04), mapped from its proposed contract to our schema as its README asks.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.documents import AbnormalFlagSource, LabDocument, LabResult
from app.lab_extractor import extract_lab_document
from app.providers.base import ProviderError
from app.recording import ReplayProvider

ROOT = Path(__file__).resolve().parent.parent
DOC_CASES_DIR = ROOT / "fixtures" / "doc_cases"
DOCUMENTS_DIR = ROOT / "fixtures" / "documents"

# Classes where the correct behaviour is to withhold rather than assert: an
# unreadable value, or a flag the lab did not print.
_RESTRAINT_CLASSES = frozenset({"degraded_scan", "no_printed_flag"})


@dataclass
class DocCaseResult:
    name: str
    scores: dict[str, bool | None]
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def load_doc_cases(directory: Path = DOC_CASES_DIR) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]


def _norm(text: str | None) -> str:
    return "".join((text or "").split()).lower()


def _value_text(value: object) -> str | None:
    if value is None:
        return None
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def _target(doc: LabDocument, needle: str) -> LabResult | None:
    return next((r for r in doc.results if needle.lower() in r.test_name.lower()), None)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.buf = io.StringIO()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buf.write(self.format(record) + "\n")
        except Exception:  # pragma: no cover
            self.buf.write(str(record.msg) + "\n")


async def _extract(case: dict[str, Any], model: str) -> LabDocument:
    pdf = (DOCUMENTS_DIR / case["pdf"]).read_bytes()
    return await extract_lab_document(
        document_id=case["document_id"],
        pdf_bytes=pdf,
        media_type="application/pdf",
        provider=ReplayProvider(case["case_id"], model),
    )


def score_doc_case(case: dict[str, Any], *, model: str) -> DocCaseResult:
    """Replay one recorded extraction and score it on the five rubrics."""
    name = case["case_id"]
    expect = case["expect"]
    failures: list[str] = []

    root = logging.getLogger()
    cap = _Capture()
    level = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    try:
        doc = asyncio.run(_extract(case, model))
    except ProviderError as exc:
        # Includes StaleRecordingError: the recording no longer describes this
        # code. That is a failure of evidence, not something to paper over.
        return DocCaseResult(
            name,
            {"schema_valid": False, "citation_present": None, "factually_consistent": None,
             "safe_refusal": None, "no_phi_in_logs": None},
            [f"no valid replay: {exc}"],
        )
    finally:
        root.removeHandler(cap)
        root.setLevel(level)
    log_text = cap.buf.getvalue()

    target = _target(doc, expect["test_name_contains"])

    # schema_valid - we have a validated LabDocument and the expected test is in it.
    schema_valid = target is not None
    if not schema_valid:
        failures.append(f"no result named like {expect['test_name_contains']!r}")

    # citation_present - every value we report is traceable to printed text.
    citation_present = True
    for r in doc.results:
        v = _value_text(r.value)
        if v is not None and v not in r.citation.quote_or_value:
            citation_present = False
            failures.append(f"{r.test_name}: value {v} not found in its cited text {r.citation.quote_or_value!r}")

    # factually_consistent - the fields the case pins down.
    factual = True
    if target is not None:
        got = _value_text(target.value)
        if got not in expect["allowed_values"]:
            factual = False
            failures.append(f"value {got!r} not in allowed {expect['allowed_values']!r}")
        if got is not None and "unit" in expect and _norm(target.unit) != _norm(expect["unit"]):
            factual = False
            failures.append(f"unit {target.unit!r} != {expect['unit']!r}")
        if "reference_range" in expect and _norm(target.reference_range) != _norm(expect["reference_range"]):
            factual = False
            failures.append(f"range {target.reference_range!r} != {expect['reference_range']!r}")
        if "collection_date" in expect:
            got_date = target.collection_date or doc.collection_date
            if str(got_date) != expect["collection_date"]:
                factual = False
                failures.append(f"collection date {got_date} != {expect['collection_date']}")
        if "printed_flag" in expect:
            printed = target.abnormal_flag.value if (
                target.abnormal_flag is not None and target.abnormal_flag_source is AbnormalFlagSource.EXTRACTED
            ) else None
            if printed != expect["printed_flag"]:
                factual = False
                failures.append(f"printed flag {printed!r} != {expect['printed_flag']!r}")
    else:
        factual = False

    # safe_refusal - where restraint is the right answer, restraint happened.
    safe: bool | None = None
    if case["test_class"] in _RESTRAINT_CLASSES:
        safe = True
        if case["test_class"] == "degraded_scan" and target is not None:
            if _value_text(target.value) not in expect["allowed_values"]:
                safe = False
                failures.append("invented a value for an unreadable result")
        if case["test_class"] == "no_printed_flag":
            invented = [r.test_name for r in doc.results if r.abnormal_flag_source is AbnormalFlagSource.EXTRACTED]
            if invented:
                safe = False
                failures.append(f"reported a printed flag the lab never printed: {invented}")

    # no_phi_in_logs - nothing the document says reaches a log.
    pdf_b64 = base64.b64encode((DOCUMENTS_DIR / case["pdf"]).read_bytes()).decode()
    canaries = [pdf_b64[:64]] + [r.citation.quote_or_value for r in doc.results if len(r.citation.quote_or_value) >= 6]
    leaked = [c for c in canaries if c and c in log_text]
    no_phi = not leaked
    if leaked:
        failures.append(f"{len(leaked)} document string(s) reached a log")

    return DocCaseResult(
        name,
        {"schema_valid": schema_valid, "citation_present": citation_present,
         "factually_consistent": factual, "safe_refusal": safe, "no_phi_in_logs": no_phi},
        failures,
    )


__all__ = ["DOC_CASES_DIR", "DocCaseResult", "load_doc_cases", "score_doc_case"]
