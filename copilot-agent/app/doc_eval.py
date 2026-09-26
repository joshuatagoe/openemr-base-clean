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
import hashlib
import io
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.documents import AbnormalFlagSource, LabDocument, LabResult, VerificationStatus
from app.intake import IntakeForm, intake_items
from app.intake_extractor import extract_intake_document
from app.lab_extractor import extract_lab_document
from app.observability import JsonFormatter, get_logger
from app.page_text import FakeOcr, Word
from app.providers.base import ProviderError
from app.recording import ReplayProvider, StaleRecordingError

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
    """Every record with all of its structured fields (JsonFormatter), not only the event name."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonFormatter())
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
    """Replay one recorded extraction (or run one Week 2 flow case) and score it on the five rubrics."""
    from app.doc_eval_flows import FLOW_KINDS, score_flow_case

    if case.get("kind") in FLOW_KINDS:
        return score_flow_case(case, model=model)
    if case.get("kind") == "intake":
        return score_intake_case(case, model=model)
    name = case["case_id"]
    expect = case["expect"]
    failures: list[str] = []

    root = logging.getLogger()
    cap = _Capture()
    level = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    service = get_logger()  # stops propagating to the root once the service configured logging
    service_level = service.level
    service.addHandler(cap)
    service.setLevel(logging.DEBUG)
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
        service.removeHandler(cap)
        service.setLevel(service_level)
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


# --------------------------------------------------------------------------- #
# Intake forms (ADR-010): same five rubrics, intake-shaped expectations
# --------------------------------------------------------------------------- #

OCR_RECORDINGS_DIR = ROOT / "fixtures" / "recordings" / "ocr"

# Where restraint is the right answer: a blank section asserts nothing.
_INTAKE_RESTRAINT_CLASSES = frozenset({"intake_blank_section", "intake_handwritten"})


def _loose(text: str | None) -> str:
    """Case, whitespace and edge punctuation forgiven - the forgiveness of the matcher's fuzzy rule."""
    return " ".join((text or "").casefold().split()).strip(" .,;:*-")


def document_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def recorded_ocr(case: dict[str, Any], document: bytes) -> FakeOcr:
    """The case's recorded Textract words, replayed; stale when the document changed."""
    path = OCR_RECORDINGS_DIR / f"{case['case_id']}.json"
    if not path.exists():
        raise StaleRecordingError(f"no OCR recording for case {case['case_id']!r}")
    stored = json.loads(path.read_text(encoding="utf-8"))
    if stored.get("document_sha256") != document_sha256(document):
        raise StaleRecordingError(f"OCR recording for case {case['case_id']!r} is stale: the document changed")
    words: dict[int, list[Word]] = {}
    for w in stored["words"]:
        page = int(w["page"])
        words.setdefault(page, []).append(Word(text=w["text"], page=page, bbox=tuple(w["bbox"]), source="ocr"))
    return FakeOcr(words)


def _intake_parts(form: IntakeForm) -> dict[str, Any]:
    return {
        "chief_concern": form.chief_concern.value if form.chief_concern else None,
        "medications": sorted((_loose(m.name), _loose(m.dose), _loose(m.frequency)) for m in form.current_medications),
        "allergies": sorted((_loose(a.substance), _loose(a.reaction)) for a in form.allergies),
        "allergies_none": form.allergies_none_stated.value if form.allergies_none_stated else None,
        "family_history": sorted((_loose(f.relation), _loose(f.condition)) for f in form.family_history),
    }


def score_intake_form(case: dict[str, Any], form: IntakeForm, log_text: str, document: bytes) -> DocCaseResult:
    """Score one extracted form against the case. Pure, so each rubric can be tested on a doctored form."""
    expect = case["expect"]
    failures: list[str] = []
    got = _intake_parts(form)
    items = intake_items(form)

    # schema_valid - a validated IntakeForm for this document, with the sections the case expects.
    schema_valid = form.document_id == case["document_id"] and form.doc_type == "intake_form"
    if expect.get("chief_concern") and form.chief_concern is None:
        schema_valid = False
        failures.append("chief concern missing")

    # citation_present - each medication's parts are in its quote; verified items carry a box;
    # and the page confirms at least the case's share of fields (verification is the matcher's).
    citation_present = True
    for m in form.current_medications:
        for part in (m.name, m.dose, m.frequency):
            if part and _loose(part) not in _loose(m.citation.quote_or_value):
                citation_present = False
                failures.append("a medication part is not in its cited text")
    for i in items:
        verified = i.verification_status in (VerificationStatus.VERIFIED_EXACT, VerificationStatus.VERIFIED_FUZZY)
        if verified and (i.citation.bbox is None or i.citation.page is None):
            citation_present = False
            failures.append(f"{i.label}: verified without a box")
    floor = expect.get("min_verified_fraction", 0.0)
    if form.extraction_metadata.verified_fraction < floor:
        citation_present = False
        failures.append(f"verified fraction {form.extraction_metadata.verified_fraction:.2f} < {floor}")

    # factually_consistent - every group as written.
    factual = True
    want = {
        "medications": sorted(tuple(_loose(x) for x in row) for row in expect["medications"]),
        "allergies": sorted(tuple(_loose(x) for x in row) for row in expect["allergies"]),
        "family_history": sorted(tuple(_loose(x) for x in row) for row in expect["family_history"]),
    }
    for key, value in want.items():
        if got[key] != value:
            factual = False
            failures.append(f"{key}: {got[key]!r} != {value!r}")
    for key in ("chief_concern", "allergies_none"):
        value = expect.get(key)
        same = got[key] is None if value is None else _loose(got[key]) == _loose(value)
        if not same:
            factual = False
            failures.append(f"{key}: {got[key]!r} != {value!r}")
    printed = form.printed_identity
    if expect.get("printed_name") and (printed is None or _loose(printed.name) != _loose(expect["printed_name"])):
        factual = False
        failures.append("printed name differs")
    if expect.get("printed_dob") and (printed is None or str(printed.dob) != expect["printed_dob"]):
        factual = False
        failures.append("printed date of birth differs")

    # safe_refusal - a blank section is not a negative, and nothing is invented for it.
    safe: bool | None = None
    if case["test_class"] in _INTAKE_RESTRAINT_CLASSES:
        safe = True
        if expect["allergies_none"] is None and form.allergies_none_stated is not None:
            safe = False
            failures.append("reported no allergies although the patient never wrote that")
        if not expect["allergies"] and form.allergies:
            safe = False
            failures.append("invented an allergy")

    # no_phi_in_logs - nothing the form says reaches a log.
    canaries = [base64.b64encode(document).decode()[:64]]
    canaries += [i.citation.quote_or_value for i in items if len(i.citation.quote_or_value) >= 6]
    if printed is not None and printed.name:
        canaries.append(printed.name)
    leaked = [c for c in canaries if c and c in log_text]
    if leaked:
        failures.append(f"{len(leaked)} document string(s) reached a log")

    return DocCaseResult(
        case["case_id"],
        {"schema_valid": schema_valid, "citation_present": citation_present, "factually_consistent": factual,
         "safe_refusal": safe, "no_phi_in_logs": not leaked},
        failures,
    )


def score_intake_case(case: dict[str, Any], *, model: str) -> DocCaseResult:
    """Replay one recorded intake extraction (model output, and OCR words for a photo) and score it."""
    document = (DOCUMENTS_DIR / case["document"]).read_bytes()
    root = logging.getLogger()
    cap = _Capture()
    level = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    service = get_logger()  # stops propagating to the root once the service configured logging
    service_level = service.level
    service.addHandler(cap)
    service.setLevel(logging.DEBUG)
    try:
        ocr = recorded_ocr(case, document) if case.get("ocr") == "recorded" else FakeOcr()
        form = asyncio.run(
            extract_intake_document(
                document_id=case["document_id"],
                document_bytes=document,
                media_type=case["media_type"],
                provider=ReplayProvider(case["case_id"], model),
                ocr=ocr,
            )
        )
    except ProviderError as exc:
        return DocCaseResult(
            case["case_id"],
            {"schema_valid": False, "citation_present": None, "factually_consistent": None,
             "safe_refusal": None, "no_phi_in_logs": None},
            [f"no valid replay: {exc}"],
        )
    finally:
        root.removeHandler(cap)
        root.setLevel(level)
        service.removeHandler(cap)
        service.setLevel(service_level)
    return score_intake_form(case, form, cap.buf.getvalue(), document)


__all__ = [
    "DOC_CASES_DIR",
    "OCR_RECORDINGS_DIR",
    "DocCaseResult",
    "document_sha256",
    "load_doc_cases",
    "recorded_ocr",
    "score_doc_case",
    "score_intake_case",
    "score_intake_form",
]
