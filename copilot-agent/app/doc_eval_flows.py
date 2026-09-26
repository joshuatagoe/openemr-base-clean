"""Week 2 flow cases for the golden set: the briefing, extract and follow-up paths end to end.

The extraction cases in ``fixtures/doc_cases`` score what the model read. These
score what the physician is shown from it, through the same code the routes
run, offline:

``kind: "briefing"``  stored extractions (each replayed from an extraction
    case's own recording and stored as the module stores it) -> the supervised
    briefing graph, the answer model being the deterministic stub. Nothing is
    re-extracted: an extraction model call fails the case.
``kind: "extract"``   one document through ``run_document_extract``, the route
    the module calls, replayed from a recording.
``kind: "followup"``  a follow-up turn over a bundle carrying pending document
    facts, with a scripted model turn (as the Week 1 cases script theirs), so
    the verifier's rules are what is scored.

Each case names the one rubric its expectations answer to (``rubric``); the
other categories are scored generically (the response validates, every line's
citation resolves to something the request supplied, nothing sensitive reached
a log). ``trace: true`` also runs the case with Langfuse tracing on, exporting
to memory, and checks every exported span attribute for the same strings.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.briefing import COMPUTED_LABEL, PRINTED_FLAG_LABEL, BriefingLine
from app.contracts import ADVICE_REFUSAL_TEXT, SCOPE_REFUSAL_TEXT, ContextBundle
from app.documents import LabDocument
from app.intake import IntakeForm, intake_items
from app.observability import JsonFormatter, get_logger, span
from app.providers.base import ModelStatement, ModelTurnAnswer, ModelUsage, ProviderError, ToolCall, TurnStep
from app.providers.stub_provider import StubProvider
from app.recording import ReplayProvider

ROOT = Path(__file__).resolve().parent.parent
DOC_CASES_DIR = ROOT / "fixtures" / "doc_cases"
DOCUMENTS_DIR = ROOT / "fixtures" / "documents"
FOLLOWUP_BUNDLE = ROOT / "fixtures" / "lab_followup.json"

#: A synthetic patient uuid; a canary in every flow case.
PATIENT_UUID = "a2c3ab57-cdd6-4aad-afc9-e19c171e7ed7"

FLOW_KINDS = frozenset({"briefing", "extract", "followup"})
_CATEGORIES = ("schema_valid", "citation_present", "factually_consistent", "safe_refusal", "no_phi_in_logs")
# Shorter strings are too common to be evidence of a leak ("mg", "8.9", a date part).
_MIN_CANARY = 6


# --------------------------------------------------------------------------- #
# Capture: logs with every structured field, and (optionally) exported spans
# --------------------------------------------------------------------------- #


class _JsonCapture(logging.Handler):
    """Every record, formatted with all of its structured fields - not just the event name."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.buf = io.StringIO()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buf.write(self.format(record) + "\n")
        except Exception:  # pragma: no cover - a broken record must not hide a leak
            self.buf.write(str(record.__dict__) + "\n")


@contextmanager
def capture_logs() -> Iterator[_JsonCapture]:
    """Attach to the root and to the service logger (which stops propagating once configured)."""
    cap = _JsonCapture()
    loggers = [logging.getLogger(), get_logger()]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(cap)
        lg.setLevel(logging.DEBUG)
    try:
        yield cap
    finally:
        for lg, level in zip(loggers, levels, strict=True):
            lg.removeHandler(cap)
            lg.setLevel(level)


@contextmanager
def exported_spans() -> Iterator[list[Any]]:
    """Langfuse tracing on, exporting to memory through the production mask; yields the spans after."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app import observability

    exporter = InMemorySpanExporter()
    spans: list[Any] = []
    previous = os.environ.get("LANGFUSE_TRACING_ENABLED")
    os.environ["LANGFUSE_TRACING_ENABLED"] = "true"
    observability.configure_tracing(
        enabled=True,
        public_key=f"pk-lf-eval-{uuid.uuid4().hex}",  # the SDK caches resources per public key
        secret_key="sk-lf-eval",
        base_url="http://127.0.0.1:9",
        span_exporter=exporter,
    )
    try:
        yield spans
    finally:
        if observability._langfuse is not None:  # noqa: SLF001
            observability._langfuse.flush()  # noqa: SLF001
        spans.extend(exporter.get_finished_spans())
        observability.shutdown_tracing()
        if previous is None:
            os.environ.pop("LANGFUSE_TRACING_ENABLED", None)
        else:
            os.environ["LANGFUSE_TRACING_ENABLED"] = previous


def _span_text(spans: list[Any]) -> str:
    return "\n".join(json.dumps(dict(s.attributes or {}), default=str) for s in spans)


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #


class _Checks:
    """Failures filed under a rubric. Expectations go to the case's own rubric."""

    def __init__(self, rubric: str) -> None:
        if rubric not in _CATEGORIES:
            raise ValueError(f"unknown rubric {rubric!r}")
        self.rubric = rubric
        self.failed: dict[str, list[str]] = {c: [] for c in _CATEGORIES}

    def fail(self, category: str, message: str) -> None:
        self.failed[category].append(message)

    def expect(self, ok: bool, message: str) -> None:
        if not ok:
            self.failed[self.rubric].append(message)

    def result(self, name: str) -> Any:
        from app.doc_eval import DocCaseResult

        scores: dict[str, bool | None] = {c: not self.failed[c] for c in _CATEGORIES}
        if self.rubric != "safe_refusal" and not self.failed["safe_refusal"]:
            scores["safe_refusal"] = None  # restraint is scored only where restraint is the point
        failures = [f"{c}: {m}" for c in _CATEGORIES for m in self.failed[c]]
        return DocCaseResult(name, scores, failures)


def _no_replay(name: str, rubric: str, exc: Exception) -> Any:
    from app.doc_eval import DocCaseResult

    scores: dict[str, bool | None] = dict.fromkeys(_CATEGORIES)
    scores["schema_valid"] = False
    return DocCaseResult(name, scores, [f"no valid replay: {exc}"])


def _leaks(checks: _Checks, canaries: set[str], log_text: str, span_text: str | None) -> None:
    for c in sorted(canaries):
        if len(c) < _MIN_CANARY:
            continue
        if c in log_text:
            checks.fail("no_phi_in_logs", f"a document or patient string reached a log ({len(c)} chars)")
        if span_text is not None and c in span_text:
            checks.fail("no_phi_in_logs", f"a document or patient string reached an exported span ({len(c)} chars)")


# --------------------------------------------------------------------------- #
# Stored documents, replayed from the extraction cases' own recordings
# --------------------------------------------------------------------------- #


def _source_case(case_id: str) -> dict[str, Any]:
    return json.loads((DOC_CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8"))


def _document_bytes(src: dict[str, Any]) -> bytes:
    return (DOCUMENTS_DIR / (src.get("document") or src["pdf"])).read_bytes()


async def _replayed(src: dict[str, Any], model: str) -> LabDocument | IntakeForm:
    from app.doc_eval import recorded_ocr
    from app.intake_extractor import extract_intake_document
    from app.lab_extractor import extract_lab_document
    from app.page_text import FakeOcr

    data = _document_bytes(src)
    provider = ReplayProvider(src["case_id"], model)
    if src.get("kind") == "intake":
        ocr = recorded_ocr(src, data) if src.get("ocr") == "recorded" else FakeOcr()
        return await extract_intake_document(
            document_id=src["document_id"], document_bytes=data, media_type=src["media_type"], provider=provider, ocr=ocr
        )
    return await extract_lab_document(document_id=src["document_id"], pdf_bytes=data, media_type="application/pdf", provider=provider)


def _document_canaries(src: dict[str, Any], document: LabDocument | IntakeForm) -> set[str]:
    out = {base64.b64encode(_document_bytes(src)).decode()[:64]}
    if isinstance(document, IntakeForm):
        out |= {i.citation.quote_or_value for i in intake_items(document)}
        out |= {m.name for m in document.current_medications if m.name}
    else:
        out |= {r.citation.quote_or_value for r in document.results}
        out |= {r.test_name for r in document.results}
    printed = document.printed_identity
    if printed is not None and printed.name:
        out.add(printed.name)
    return out


def _stored(spec: dict[str, Any], document: LabDocument | IntakeForm) -> dict[str, Any]:
    """The extraction as the module stores and sends it (ADR-012), with its review filter applied."""
    from app.document_briefing import stored_form

    if isinstance(document, IntakeForm):
        return {"document_id": document.document_id, "doc_type": "intake_form", "extraction": stored_form(document).model_dump(mode="json")}
    # The module sends only values still waiting for review: filed ones arrive as prior facts,
    # rejected and un-filed ones not at all (DocumentBriefingController, PHPUnit-tested).
    reviewed = [s.casefold() for s in spec.get("reviewed", [])]
    kept = [r for r in document.results if not any(s in r.test_name.casefold() for s in reviewed)]
    stored = document.model_copy(update={"printed_identity": None, "results": kept})
    return {"document_id": document.document_id, "doc_type": "lab_pdf", "extraction": stored.model_dump(mode="json")}


class _AnswerModelOnly(StubProvider):
    """The deterministic answer model; any other model call is recorded (a stored document must not be re-read)."""

    def __init__(self) -> None:
        super().__init__()
        self.schemas: list[str] = []

    async def parse_structured(self, *, schema: Any, **kwargs: Any) -> Any:
        self.schemas.append(schema.__name__)
        from app.document_briefing import ConsiderationDraftSet

        if schema is not ConsiderationDraftSet:
            raise ProviderError("a stored document was sent to an extraction model")
        return await super().parse_structured(schema=schema, **kwargs)


# --------------------------------------------------------------------------- #
# kind: briefing
# --------------------------------------------------------------------------- #


def _all_lines(briefing: Any) -> list[tuple[str, BriefingLine]]:
    return [("what_changed", ln) for ln in briefing.what_changed] + [("needs_attention", ln) for ln in briefing.needs_attention]


def _shown_text(briefing: Any) -> list[str]:
    texts = [ln.text for _, ln in _all_lines(briefing)]
    for c in briefing.what_to_consider:
        texts += [c.text, c.relevance, c.uncertainty or ""] + [f.text for f in c.facts]
    return texts


def _line_matches(section: str, line: BriefingLine, want: dict[str, Any]) -> bool:
    if want.get("section") not in (None, section):
        return False
    if want.get("tier") not in (None, line.tier.value):
        return False
    if any(s not in line.text for s in want.get("text_contains", [])):
        return False
    if any(s.casefold() in line.text.casefold() for s in want.get("text_absent", [])):
        return False
    if "record_citation" in want and (line.record_citation.record_id if line.record_citation else None) != want["record_citation"]:
        return False
    if "document_source" in want and (line.document_citation.source_id if line.document_citation else None) != want["document_source"]:
        return False
    if "flag_source" in want and (line.abnormal_flag_source.value if line.abnormal_flag_source else None) != want["flag_source"]:
        return False
    if "computed" in want and (line.computed is not None) != want["computed"]:
        return False
    return want.get("not_yet_in_chart") in (None, line.not_yet_in_chart)


def _rendered_block(rendered: str, line: BriefingLine) -> str:
    """The rendered lines for one briefing line: its text and the indented rows under it."""
    rows = rendered.splitlines()
    for i, row in enumerate(rows):
        if row.startswith("  - ") and line.text in row:
            block = [row]
            for nxt in rows[i + 1:]:
                if not nxt.startswith("      "):
                    break
                block.append(nxt)
            return "\n".join(block)
    return ""


async def _run_briefing(case: dict[str, Any], model: str) -> tuple[Any, Any, _AnswerModelOnly, set[str], set[str]]:
    from app.document_briefing import DocumentBriefingRequest, build_reranker
    from app.workflow import DOCUMENT_BRIEFING_BUDGET_SECONDS, MAX_ROUTING_STEPS, run_supervised_briefing

    documents: list[dict[str, Any]] = []
    canaries: set[str] = {PATIENT_UUID}
    for spec in case["documents"]:
        src = _source_case(spec["from_case"])
        document = await _replayed(src, model)
        canaries |= _document_canaries(src, document)
        documents.append(_stored(spec, document))
    chart_meds = case.get("chart_medications")
    canaries |= {m["drug_name"] for m in chart_meds or []}
    request = DocumentBriefingRequest.model_validate({
        "correlation_id": str(uuid.uuid4()),
        "patient_uuid": PATIENT_UUID,
        "documents": documents,
        "prior_facts": case.get("prior_facts", []),
        "chart_medications": chart_meds,
        "question": case.get("question"),
    })
    known_records = {f.result_id for f in request.prior_facts} | {m.record_id for m in request.chart_medications or []}
    provider = _AnswerModelOnly()
    # The route's own encounter span (run_document_briefing), so a traced case exports one trace;
    # the graph is called directly so a case can set the budget and the step cap.
    with span("document_briefing", cid=request.correlation_id):
        response = await run_supervised_briefing(
            request,
            provider=provider,
            reranker=build_reranker("fake", region="us-east-1"),
            budget_seconds=case.get("budget_seconds", DOCUMENT_BRIEFING_BUDGET_SECONDS),
            max_steps=case.get("max_steps", MAX_ROUTING_STEPS),
        )
    return request, response, provider, canaries, known_records


def score_briefing_case(case: dict[str, Any], *, model: str) -> Any:
    name, expect = case["case_id"], case["expect"]
    checks = _Checks(case["rubric"])
    spans: list[Any] | None = None
    with capture_logs() as cap:
        try:
            if case.get("trace"):
                with exported_spans() as spans:
                    request, response, provider, canaries, known = asyncio.run(_run_briefing(case, model))
            else:
                request, response, provider, canaries, known = asyncio.run(_run_briefing(case, model))
        except ProviderError as exc:  # includes a stale recording
            return _no_replay(name, case["rubric"], exc)
    if spans is not None and not spans:
        checks.fail("schema_valid", "tracing was on but no span was exported")

    # schema_valid - the panel's contract, and the status/reason the case expects.
    try:
        type(response).model_validate(response.model_dump(mode="json"))
    except ValueError as exc:  # pragma: no cover - the graph builds validated models
        checks.fail("schema_valid", f"response does not validate: {exc}")
    checks.expect(response.status.value == expect.get("status", "ok"), f"status {response.status.value!r} != {expect.get('status', 'ok')!r}")
    if "degraded_reason" in expect:
        checks.expect(response.degraded_reason == expect["degraded_reason"], f"degraded_reason {response.degraded_reason!r} != {expect['degraded_reason']!r}")

    # Routing (CR4): the path the supervisor took, and which models it called.
    targets = [d.target for d in response.routing]
    if "routing_targets" in expect:
        checks.expect(targets == expect["routing_targets"], f"routing {targets} != {expect['routing_targets']}")
    for target in expect.get("routing_excludes", []):
        checks.expect(target not in targets, f"routing took {target!r}")
    if "last_reason_code" in expect:
        last = response.routing[-1].reason_code if response.routing else None
        checks.expect(last == expect["last_reason_code"], f"last routing reason {last!r} != {expect['last_reason_code']!r}")
    if "model_calls" in expect:
        checks.expect(provider.schemas == expect["model_calls"], f"model calls {provider.schemas} != {expect['model_calls']}")

    briefing = response.briefing
    if briefing is None:
        checks.expect(not expect.get("lines"), "no briefing was produced")
    else:
        lines = _all_lines(briefing)
        # citation_present - every shown line cites something this request supplied.
        ids = {str(i) for i in request.document_ids}
        for _, line in lines:
            if line.document_citation is not None and line.document_citation.source_id not in ids:
                checks.fail("citation_present", f"{line.line_id} cites a document not in the request")
            if line.record_citation is not None and line.record_citation.record_id not in known:
                checks.fail("citation_present", f"{line.line_id} cites a chart record not in the request")
        for c in briefing.what_to_consider:
            if not c.citations or not all(f.document_citation or f.record_citation for f in c.facts):
                checks.fail("citation_present", f"consideration {c.consideration_id} is not fully cited")

        for want in expect.get("lines", []):
            hits = [(s, ln) for s, ln in lines if _line_matches(s, ln, want)]
            count = want.get("count")
            ok = len(hits) == count if count is not None else bool(hits)
            checks.expect(ok, f"expected {count if count is not None else '>=1'} line(s) like {want}, found {len(hits)}")
            for _, ln in hits:
                block = _rendered_block(response.rendered_text, ln)
                for label in want.get("rendered_with", []):
                    checks.expect(label in block, f"{ln.line_id} is not rendered with {label!r}")
                for label in want.get("rendered_without", []):
                    checks.expect(label not in block, f"{ln.line_id} is rendered with {label!r}")
        shown = "\n".join(_shown_text(briefing)).casefold()
        rendered = response.rendered_text.casefold()
        for s in expect.get("never_shown", []):
            checks.expect(s.casefold() not in shown and s.casefold() not in rendered, f"{s!r} reached the briefing")
        for s in expect.get("limitations_contain", []):
            checks.expect(any(s in lim for lim in briefing.limitations), f"no limitation mentions {s!r}")
        if "refusal" in expect:
            want_refusal = {"advice": ADVICE_REFUSAL_TEXT, "scope": SCOPE_REFUSAL_TEXT, None: None}[expect["refusal"]]
            checks.expect(briefing.refusal == want_refusal, f"refusal {briefing.refusal!r} != {want_refusal!r}")
        canaries |= {ln.text for _, ln in lines}

    _leaks(checks, canaries, cap.buf.getvalue(), _span_text(spans) if spans is not None else None)
    return checks.result(name)


# --------------------------------------------------------------------------- #
# kind: extract
# --------------------------------------------------------------------------- #


def score_extract_case(case: dict[str, Any], *, model: str) -> Any:
    from app.document_briefing import DocumentExtractRequest, run_document_extract

    name, expect = case["case_id"], case["expect"]
    checks = _Checks(case["rubric"])
    src = _source_case(case["from_case"])
    data = _document_bytes(src)
    request = DocumentExtractRequest(
        correlation_id=uuid.uuid4(),
        patient_uuid=uuid.UUID(PATIENT_UUID),
        document_id=src["document_id"],
        doc_type="intake_form" if src.get("kind") == "intake" else "lab_pdf",
        media_type=src.get("media_type", "application/pdf"),
        document_base64=base64.b64encode(data).decode(),
    )
    with capture_logs() as cap, exported_spans() as spans:
        response = asyncio.run(run_document_extract(request, provider=ReplayProvider(src["case_id"], model)))
    if response.status != "ok" or response.extraction is None:
        return _no_replay(name, case["rubric"], ProviderError(f"extract degraded: {response.degraded_reason}"))
    if not spans:
        checks.fail("schema_valid", "tracing was on but no span was exported")

    extraction = response.extraction
    printed = response.printed_identity
    # C2 / ADR-012: the printed identity goes to the module once, top-level, never inside what it stores.
    checks.expect(extraction.printed_identity is None, "the stored extraction carries the printed identity")
    if isinstance(extraction, IntakeForm):
        checks.expect(extraction.demographics.name is None and extraction.demographics.date_of_birth is None,
                      "the stored intake form carries the written name or date of birth")
    if expect.get("printed_identity_returned"):
        checks.expect(printed is not None and bool(printed.name), "the printed identity was not returned for the module's check")

    canaries = {PATIENT_UUID, request.document_base64[:64]}
    if printed is not None:
        canaries |= {printed.name or "", str(printed.dob) if printed.dob else ""}
    if isinstance(extraction, IntakeForm):
        canaries |= {i.citation.quote_or_value for i in intake_items(extraction)}
    else:
        canaries |= {r.citation.quote_or_value for r in extraction.results} | {r.test_name for r in extraction.results}
    _leaks(checks, canaries, cap.buf.getvalue(), _span_text(spans))
    return checks.result(name)


# --------------------------------------------------------------------------- #
# kind: followup
# --------------------------------------------------------------------------- #


class ScriptedTurnProvider:
    """Plays a case's scripted model turn: tool-call steps, then an answer. No network."""

    name = "scripted"

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self._steps = list(steps)

    @staticmethod
    def _usage() -> ModelUsage:
        return ModelUsage(provider="scripted", model="scripted", input_tokens=0, output_tokens=0, latency_ms=0)

    async def turn_step(self, system: str, transcript: list[Any], tools: list[dict[str, Any]], *, force_answer: bool) -> TurnStep:
        step = self._steps.pop(0) if self._steps else {"answer": []}
        if "answer" in step or force_answer:
            statements = [
                ModelStatement(text=s["text"], kind=s["kind"], citation_record_ids=list(s.get("cited", [])))
                for s in step.get("answer", [])
            ]
            return TurnStep(answer=ModelTurnAnswer(statements=statements), usage=self._usage(), assistant_content=[{"type": "text", "text": "answer"}])
        calls = [ToolCall(call_id=f"call-{i}", name=c["name"], arguments=c.get("arguments", {})) for i, c in enumerate(step["tool_calls"], 1)]
        return TurnStep(tool_calls=calls, usage=self._usage(), assistant_content=[{"type": "tool_use", "id": c.call_id, "name": c.name, "input": c.arguments} for c in calls])

    @staticmethod
    def tool_results_message(results: list[tuple[str, str]]) -> dict[str, Any]:
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": content} for cid, content in results]}

    async def ping(self) -> bool:
        return True


def _followup_bundle(case: dict[str, Any]) -> ContextBundle:
    data = json.loads(FOLLOWUP_BUNDLE.read_text(encoding="utf-8"))["context"]
    data.update(case.get("bundle_update", {}))
    return ContextBundle.model_validate(data)


def score_followup_case(case: dict[str, Any], *, model: str) -> Any:  # noqa: ARG001 - scripted, no recording
    from app.followup import run_turn

    name, expect = case["case_id"], case["expect"]
    checks = _Checks(case["rubric"])
    bundle = _followup_bundle(case)
    spans: list[Any] | None = None
    async def turn() -> Any:
        with span("turn", cid=bundle.correlation_id, turn_index=0):  # the route's span (app.main)
            return await run_turn(ScriptedTurnProvider(case["script"]), bundle, [], [], case["question"])

    with capture_logs() as cap:
        if case.get("trace"):
            with exported_spans() as spans:
                outcome = asyncio.run(turn())
        else:
            outcome = asyncio.run(turn())
    if spans is not None and not spans:
        checks.fail("schema_valid", "tracing was on but no span was exported")

    known = {r.result_id for r in bundle.lab_results} | {f.fact_id for f in bundle.pending_document_facts}
    for s in outcome.statements:
        for c in s.citations:
            if c.record_id not in known:
                checks.fail("citation_present", f"a kept statement cites {c.record_id!r}, not in the bundle")

    for want in expect.get("kept", []):
        hits = [
            s for s in outcome.statements
            if s.kind.value == want.get("kind", s.kind.value)
            and all(t in s.text for t in want.get("text_contains", []))
            and ("cited" not in want or sorted(c.record_id for c in s.citations) == sorted(want["cited"]))
            and ("cited_types" not in want or sorted(c.record_type.value for c in s.citations) == sorted(want["cited_types"]))
        ]
        checks.expect(bool(hits), f"no kept statement like {want}")
    if "kept_count" in expect:
        checks.expect(len(outcome.statements) == expect["kept_count"], f"{len(outcome.statements)} statements kept, expected {expect['kept_count']}")
    if "rejection_codes" in expect:
        got = sorted(outcome.rejection_codes)
        checks.expect(got == sorted(expect["rejection_codes"]), f"rejections {got} != {sorted(expect['rejection_codes'])}")

    canaries = {str(bundle.patient_uuid)}
    canaries |= {f.test_name for f in bundle.pending_document_facts}
    canaries |= {s["text"] for step in case["script"] for s in step.get("answer", [])}
    canaries |= {s.text for s in outcome.statements}
    if bundle.prior_note is not None:
        canaries |= {p.strip() for p in bundle.prior_note.plan_text.split(".") if len(p.strip()) >= 12}
    _leaks(checks, canaries, cap.buf.getvalue(), _span_text(spans) if spans is not None else None)
    return checks.result(name)


def score_flow_case(case: dict[str, Any], *, model: str) -> Any:
    kind = case["kind"]
    if kind == "briefing":
        return score_briefing_case(case, model=model)
    if kind == "extract":
        return score_extract_case(case, model=model)
    return score_followup_case(case, model=model)


__all__ = [
    "FLOW_KINDS",
    "ScriptedTurnProvider",
    "capture_logs",
    "exported_spans",
    "score_briefing_case",
    "score_extract_case",
    "score_flow_case",
    "score_followup_case",
]
