"""Langfuse tracing: the PHI allow-list mask and the trace shape (ARCHITECTURE.md section 14).

The end-to-end tests run the real Langfuse SDK with an in-memory OpenTelemetry
exporter, so what is asserted on is exactly what would have been sent.
"""

from __future__ import annotations

import json
import re
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import observability
from app.main import app, get_provider_factory, get_settings
from app.observability import MASKED, TRACE_ALLOWED_KEYS, mask_for_tracing, trace_id_for
from tests.conftest import configured_settings
from tests.fakes import FakeProvider, answer, calls, hba1c, metformin, model_output, statement
from tests.test_followup import turn
from tests.test_handoff import post_bundle, read_events, ticket_for

# Strings from the synthetic fixture and the scripted answer that must never reach a trace.
CLINICAL_STRINGS = ("metformin", "HbA1c", "8.9", "Continue", "three months", "procedure_result:9001")


# --------------------------------------------------------------------------- #
# The mask
# --------------------------------------------------------------------------- #


def test_mask_replaces_everything_outside_the_allow_list() -> None:
    puuid = UUID("6f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f")
    data = {
        "plan_text": "Continue metformin. Repeat HbA1c in three months.",
        "patient_uuid": puuid,
        "statements_text": ["The latest HbA1c result was 8.9 %"],
        "nested": {"drug": "metformin", "count": 2, "ok": True, "none": None},
        "outcome": "ok",
        "reason_code": None,
        "duration_ms": 41,
        "states": ["evidence_found", "pending"],
        "cid": puuid,
        "tool_calls": ["find_results"],
        "records": 1,
    }
    masked = mask_for_tracing(data=data)
    assert masked["plan_text"] == MASKED
    assert masked["patient_uuid"] == MASKED
    assert masked["statements_text"] == [MASKED]
    assert masked["nested"] == {"drug": MASKED, "count": 2, "ok": True, "none": None}
    # allow-listed keys keep their values, including lists of codes and uuids rendered as strings
    assert masked["outcome"] == "ok" and masked["duration_ms"] == 41 and masked["reason_code"] is None
    assert masked["states"] == ["evidence_found", "pending"]
    assert masked["cid"] == str(puuid)
    assert masked["tool_calls"] == ["find_results"] and masked["records"] == 1
    # bare strings and unknown objects are replaced; numbers pass
    assert mask_for_tracing(data="Continue metformin") == MASKED
    assert mask_for_tracing(data=3.5) == 3.5
    assert mask_for_tracing(data=object()) == MASKED


def test_allow_list_has_no_clinical_or_patient_keys() -> None:
    for forbidden in ("plan_text", "patient_uuid", "puuid", "text", "question", "input", "output", "arguments", "args", "test_query"):
        assert forbidden not in TRACE_ALLOWED_KEYS


def test_trace_id_is_the_correlation_id() -> None:
    cid = "3f2b9a4e-1c7d-4e8a-9b6f-0a1b2c3d4e5f"
    assert trace_id_for(cid) == "3f2b9a4e1c7d4e8a9b6f0a1b2c3d4e5f"
    assert trace_id_for(UUID(cid)) == trace_id_for(cid)


# --------------------------------------------------------------------------- #
# Trace shape through the real SDK with an in-memory exporter
# --------------------------------------------------------------------------- #


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exp = InMemorySpanExporter()
    yield exp
    observability.shutdown_tracing()


@pytest.fixture
def traced_client(exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Configured service with tracing on and the SDK exporting to memory instead of HTTP."""
    provider = FakeProvider(
        model_output(metformin(), hba1c()),
        turn_script=[
            calls(("find_results", {"test_query": "HbA1c"})),
            answer(statement("The latest HbA1c result was 8.9 % on 2026-09-12.", "fact", "procedure_result:9001")),
        ],
    )
    original = observability.configure_tracing
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")  # the SDK reads this too; conftest turns it off
    public_key = f"pk-lf-test-{uuid4().hex}"  # the SDK caches its resources per public key

    def configure_with_memory_exporter(**kwargs: Any) -> bool:
        kwargs.update(enabled=True, public_key=public_key, secret_key="sk-lf-test", base_url="http://127.0.0.1:9", span_exporter=exporter)
        return original(**kwargs)

    monkeypatch.setattr("app.main.configure_tracing", configure_with_memory_exporter)
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    app.dependency_overrides[get_settings] = lambda: configured_settings()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_provider_factory, None)
        app.dependency_overrides.pop(get_settings, None)


def _exported(exporter: InMemorySpanExporter) -> list[Any]:
    observability._langfuse.flush()  # type: ignore[union-attr]
    return list(exporter.get_finished_spans())


def _attr_json(span: Any, key: str) -> Any:
    raw = span.attributes.get(key)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def test_briefing_and_turn_produce_one_trace_each_with_no_clinical_text(traced_client: TestClient, fixture_payload: dict, exporter: InMemorySpanExporter) -> None:
    accepted = post_bundle(traced_client, fixture_payload)
    status_code, _, events = read_events(traced_client, accepted["bundle_id"], ticket_for(accepted))
    assert status_code == 200 and events[-1][0] == "complete"
    resp = turn(traced_client, accepted["bundle_id"], ticket_for(accepted), "What was the last A1c?")
    assert resp.status_code == 200 and resp.json()["degraded"] is None

    spans = _exported(exporter)
    by_name = {s.name: s for s in spans}
    assert {"briefing", "extract", "turn", "turn_step", "tool"} <= set(by_name)

    # Trace id == correlation id, and both requests share the one trace for this cid.
    expected = trace_id_for(accepted["correlation_id"])
    assert {format(s.context.trace_id, "032x") for s in spans} == {expected}

    # Nesting: extract under briefing; turn_step and tool under turn.
    assert by_name["extract"].parent.span_id == by_name["briefing"].context.span_id
    assert by_name["tool"].parent.span_id == by_name["turn"].context.span_id
    assert all(s.parent.span_id == by_name["turn"].context.span_id for s in spans if s.name == "turn_step")

    # PHI control: nothing clinical anywhere in what would have been exported.
    for s in spans:
        blob = json.dumps(dict(s.attributes), default=str)
        for needle in CLINICAL_STRINGS:
            assert needle not in blob, (s.name, needle)
        assert accepted["patient_uuid"] not in blob, s.name

    # The generation carries usage and cost; its input/output are masked, not captured.
    extract = by_name["extract"]
    assert _attr_json(extract, "langfuse.observation.type") == "generation"
    assert _attr_json(extract, "langfuse.observation.usage_details")["output"] >= 0
    assert "total" in _attr_json(extract, "langfuse.observation.cost_details")
    assert extract.attributes.get("langfuse.observation.input") in (None, json.dumps(MASKED))

    # Outcome metadata survives the mask on the root observations.
    briefing_meta = {k.rsplit(".", 1)[-1]: v for k, v in by_name["briefing"].attributes.items() if "metadata" in k}
    assert briefing_meta.get("outcome") == "ok" and briefing_meta.get("commitments") == 2
    tool_meta = {k.rsplit(".", 1)[-1]: v for k, v in by_name["tool"].attributes.items() if "metadata" in k}
    assert tool_meta.get("tool") == "find_results" and tool_meta.get("records") == 1


def test_degraded_briefing_is_an_error_level_observation(traced_client: TestClient, fixture_payload: dict, exporter: InMemorySpanExporter) -> None:
    from app.providers.base import ProviderUnavailableError

    provider: FakeProvider = app.dependency_overrides[get_provider_factory]()()
    provider._script = [ProviderUnavailableError("upstream 503")]  # noqa: SLF001 - rescript the fake for this test
    accepted = post_bundle(traced_client, fixture_payload)
    status_code, _, events = read_events(traced_client, accepted["bundle_id"], ticket_for(accepted))
    assert status_code == 200 and events[-1][0] == "degraded"

    briefing = next(s for s in _exported(exporter) if s.name == "briefing")
    assert briefing.attributes.get("langfuse.observation.level") == "ERROR"
    assert briefing.attributes.get("langfuse.observation.status_message") == "provider_unavailable"
    blob = json.dumps(dict(briefing.attributes), default=str)
    assert "upstream 503" not in blob


def test_briefing_scores_the_north_star_proxy(client: TestClient, fixture_payload: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """KEY_METRICS section 3: a completed briefing scores briefing_verified=1; a degraded one scores 0; turns never score it."""
    seen: list[tuple[str, float | str]] = []
    monkeypatch.setattr("app.main.score", lambda name, value, data_type="NUMERIC": seen.append((name, str(value) if data_type == "CATEGORICAL" else float(value))))
    accepted = post_bundle(client, fixture_payload)
    status_code, _, events = read_events(client, accepted["bundle_id"], ticket_for(accepted))
    assert status_code == 200 and events[-1][0] == "complete"
    assert ("briefing_verified", 1.0) in seen and ("degraded", 0.0) in seen
    seen.clear()
    resp = turn(client, accepted["bundle_id"], ticket_for(accepted), "What was the last A1c?")
    assert resp.status_code == 200
    assert not any(name == "briefing_verified" for name, _ in seen), seen
    assert ("turn_success", 0.0) in seen and ("turn_outcome", "empty") in seen  # this module's fake answers with no statements

    from app.providers.base import ProviderUnavailableError

    provider: FakeProvider = app.dependency_overrides[get_provider_factory]()()
    provider._script = [ProviderUnavailableError("upstream 503")]  # noqa: SLF001
    seen.clear()
    accepted = post_bundle(client, fixture_payload, correlation_id=str(uuid4()))
    status_code, _, events = read_events(client, accepted["bundle_id"], ticket_for(accepted))
    assert status_code == 200 and events[-1][0] == "degraded"
    assert ("briefing_verified", 0.0) in seen and ("degraded", 1.0) in seen
    assert ("degraded_reason", "provider_unavailable") in seen


def test_tracing_off_by_default_leaves_the_span_seam_intact(client: TestClient, fixture_payload: dict) -> None:
    assert not observability.tracing_enabled()
    accepted = post_bundle(client, fixture_payload)
    status_code, _, events = read_events(client, accepted["bundle_id"], ticket_for(accepted))
    assert status_code == 200 and events[-1][0] == "complete"


# --------------------------------------------------------------------------- #
# Export-stage masking (``mask_otel_spans``)
#
# The legacy ``mask=`` hook only sees payloads the Langfuse SDK itself sets.
# Week 2 adds LangGraph, whose spans come from third-party OpenTelemetry
# instrumentation and never pass through it. ``mask_otel_spans`` runs in the
# exporter, so it covers every span this client exports, whatever created it.
# --------------------------------------------------------------------------- #


def _otel_span_data(attributes: dict[str, Any], *, name: str = "call_model", scope: str = "openinference.instrumentation.langchain") -> Any:
    from langfuse.types import OtelSpanData, OtelSpanIdentifier

    identifier = OtelSpanIdentifier(trace_id="0" * 32, span_id="1" * 16)
    data = OtelSpanData(
        trace_id=identifier.trace_id,
        span_id=identifier.span_id,
        parent_span_id=None,
        name=name,
        instrumentation_scope_name=scope,
        instrumentation_scope_version=None,
        attributes=dict(attributes),
        resource_attributes={"service.name": "copilot-agent"},
    )
    return identifier, data


def _masked_otel_attributes(attributes: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Run the export-stage mask over one span's attributes and return what would be exported."""
    from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult

    identifier, data = _otel_span_data(attributes, **kwargs)
    result = observability.mask_otel_spans(params=MaskOtelSpansParams(spans={identifier: data}))
    assert isinstance(result, MaskOtelSpansResult)
    patch = result.span_patches.get(identifier)
    exported = dict(attributes)
    if patch is not None:
        for key in patch.delete_attributes:
            exported.pop(key, None)
        exported.update(patch.set_attributes)
    return exported


def test_configure_tracing_installs_the_export_stage_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client masks at export, blocks foreign instrumentation, and exports through exactly one exporter."""
    exporter = InMemorySpanExporter()
    public_key = f"pk-lf-test-{uuid4().hex}"
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    assert observability.configure_tracing(
        enabled=True,
        public_key=public_key,
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:9",
        span_exporter=exporter,
    )
    try:
        resources = observability._langfuse._resources  # noqa: SLF001 - asserting on what was handed to the SDK
        assert resources.mask_otel_spans is observability.mask_otel_spans
        assert resources.mask is None  # the legacy hook is gone: it never saw third-party spans

        # ADR-001 section 10.3: never add a second exporter - any other exporter receives an unmasked
        # copy. This client registers one processor, and its exporter is the masking one wrapping ours.
        processors = resources.tracer_provider._active_span_processor._span_processors  # noqa: SLF001
        ours = [p for p in processors if getattr(p, "public_key", None) == public_key]
        assert len(ours) == 1, [type(p).__name__ for p in ours]
        masking_exporter = ours[0].span_exporter
        assert type(masking_exporter).__name__ == "LangfuseTransformingSpanExporter"
        assert masking_exporter._exporter is exporter  # noqa: SLF001 - the one and only sink

        # ADR-001 section 10.3: the OTel-native SDK captures any other in-process instrumentation.
        # None is installed here; anything found is refused before export rather than trusted to the mask.
        assert observability.foreign_instrumentation_scopes() == []
        assert resources.should_export_span is observability.should_export_span
    finally:
        observability.shutdown_tracing()


def test_unvouched_instrumentation_is_refused_before_export(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    def span(scope: str) -> Any:
        return SimpleNamespace(instrumentation_scope=SimpleNamespace(name=scope), attributes={"gen_ai.system": "anthropic"})

    assert observability.should_export_span(span("langfuse-sdk"))
    assert observability.should_export_span(span("openinference.instrumentation.langchain"))
    monkeypatch.setattr(observability, "_blocked_scopes", frozenset({"opentelemetry.instrumentation.requests"}))
    assert not observability.should_export_span(span("opentelemetry.instrumentation.requests"))
    assert observability.should_export_span(span("langfuse-sdk"))


def test_export_mask_allow_lists_langfuse_structure_and_masks_third_party_attributes() -> None:
    """A LangGraph/OpenInference span carries graph state under its own attribute keys: none of it survives."""
    puuid = "3b9d2c1a-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    exported = _masked_otel_attributes(
        {
            # third-party instrumentation keys - the legacy ``mask=`` never sees these
            "input.value": '{"document_text": "Hemoglobin A1c 8.9 % - Whitfield, Evelyn R."}',
            "output.value": "Continue metformin. Repeat HbA1c in three months.",
            "gen_ai.prompt.0.content": "PATIENT: Whitfield, Evelyn R.  DOB: 1981-03-14",
            "langgraph.state.patient_uuid": puuid,
            "metadata": '{"thread_id": "abc", "mrn": "7"}',
            "lab.result.value": 8.9,
            # Langfuse structure: types, codes and counts, safe to keep
            "langfuse.observation.type": "generation",
            "langfuse.observation.level": "DEFAULT",
            "langfuse.observation.usage_details": '{"input": 10, "output": 5}',
            "langfuse.observation.cost_details": '{"total": 0.0}',
            "langfuse.environment": "development",
            # Langfuse metadata keeps the week-1 allow-list semantics
            "langfuse.observation.metadata.outcome": "ok",
            "langfuse.observation.metadata.duration_ms": 41,
            "langfuse.observation.metadata.plan_text": "Continue metformin.",
            "langfuse.observation.metadata.patient_uuid": puuid,
        }
    )
    for key in ("input.value", "output.value", "gen_ai.prompt.0.content", "langgraph.state.patient_uuid", "metadata", "lab.result.value"):
        assert exported[key] == MASKED, key
    assert exported["langfuse.observation.metadata.plan_text"] == MASKED
    assert exported["langfuse.observation.metadata.patient_uuid"] == MASKED
    assert exported["langfuse.observation.type"] == "generation"
    assert exported["langfuse.observation.level"] == "DEFAULT"
    assert exported["langfuse.observation.usage_details"] == '{"input": 10, "output": 5}'
    assert exported["langfuse.observation.metadata.outcome"] == "ok"
    assert exported["langfuse.observation.metadata.duration_ms"] == 41
    assert exported["langfuse.environment"] == "development"


def test_export_mask_replaces_model_io_and_honours_the_capture_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    io = {"langfuse.observation.input": '{"prompt": "Continue metformin"}', "langfuse.observation.output": '"HbA1c 8.9 %"'}
    exported = _masked_otel_attributes(io)
    assert exported["langfuse.observation.input"] == json.dumps(MASKED)
    assert exported["langfuse.observation.output"] == json.dumps(MASKED)

    monkeypatch.setattr(observability, "_capture_io", True)  # synthetic-data evaluation runs only
    assert _masked_otel_attributes(io) == io


def test_capture_io_cannot_be_enabled_in_a_production_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch is loud and refused outside development: an accidental production flag must not open it."""
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    for environment, expected in (("development", True), ("production", False), ("prod", False), ("staging-production", False)):
        observability.configure_tracing(
            enabled=True,
            public_key=f"pk-lf-test-{uuid4().hex}",
            secret_key="sk-lf-test",
            base_url="http://127.0.0.1:9",
            environment=environment,
            capture_io=True,
            span_exporter=InMemorySpanExporter(),
        )
        try:
            assert observability.io_capture_enabled() is expected, environment
        finally:
            observability.shutdown_tracing()


# --------------------------------------------------------------------------- #
# The naming rule: ``mask_otel_spans`` cannot alter span names, span ids or
# resource attributes (ADR-001 section 10.3), so those must be opaque by rule.
# --------------------------------------------------------------------------- #


def test_span_names_tool_names_and_thread_ids_must_be_opaque_identifiers() -> None:
    for opaque in ("briefing", "extract", "turn_step", "tool", "find_results", "call_model", "3b9d2c1a-5e6f-4a7b-8c9d-0e1f2a3b4c5d", "node.route"):
        assert observability.is_opaque_identifier(opaque), opaque
    for phi in (
        "note for Whitfield, Evelyn R.",
        "HbA1c 8.9 %",
        "extract Marcus Adeyemi, MD",
        "DOB: 1981-03-14",
        "metformin 500mg BID",
        "",
        "x" * 200,
    ):
        assert not observability.is_opaque_identifier(phi), phi


def test_a_span_name_carrying_phi_is_caught_by_the_naming_rule(traced_client: TestClient, exporter: InMemorySpanExporter) -> None:
    """The name never reaches the exporter: it is replaced, and the violation is logged without it."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = observability.get_logger()  # the copilot logger does not propagate, so capture it directly
    logger.addHandler(handler)
    try:
        with observability.span("note for Whitfield, Evelyn R.", cid=str(uuid4())) as attrs:
            attrs["outcome"] = "ok"
    finally:
        logger.removeHandler(handler)

    names = {s.name for s in _exported(exporter)}
    assert observability.UNSAFE_NAME in names, names
    assert not any("Whitfield" in n for n in names), names
    events = [getattr(r, "event", "") for r in records]
    assert "tracing.unsafe_span_name" in events, events
    assert not any("Whitfield" in json.dumps(r.__dict__, default=str) for r in records)


# --------------------------------------------------------------------------- #
# Leak test: a realistic document payload through third-party instrumentation
# --------------------------------------------------------------------------- #

LAB_PDF = Path(__file__).resolve().parent.parent / "fixtures" / "documents" / "lab_hba1c_clean.pdf"

# Verbatim strings from the synthetic lab PDF and a synthetic note. None may appear in an exported span.
DOCUMENT_STRINGS = (
    "Whitfield", "Evelyn", "1981-03-14", "Marcus Adeyemi", "NORTHSIDE CLINICAL LABORATORY",
    "Hemoglobin A1c", "8.9", "164", "CLIA 45D2109876", "440 Cedar Street",
)


def test_no_document_text_base64_or_patient_identifier_reaches_an_exported_span(traced_client: TestClient, exporter: InMemorySpanExporter) -> None:
    """A third-party-instrumented span carrying a whole lab document exports nothing readable."""
    import base64

    pdf_bytes = LAB_PDF.read_bytes()
    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
    document_text = pdf_bytes.decode("latin-1")
    note = "Plan: continue metformin 500mg BID. Repeat HbA1c in three months. - Whitfield, Evelyn R., DOB 1981-03-14"
    puuid = "3b9d2c1a-5e6f-4a7b-8c9d-0e1f2a3b4c5d"

    provider = observability._langfuse._resources.tracer_provider  # noqa: SLF001
    tracer = provider.get_tracer("openinference.instrumentation.langchain")
    with tracer.start_as_current_span("call_model") as graph_span:
        graph_span.set_attribute("gen_ai.system", "anthropic")  # a gen_ai span, which the SDK exports by default
        graph_span.set_attribute("input.value", json.dumps({"document_text": document_text, "note_text": note}))
        graph_span.set_attribute("output.value", json.dumps({"results": [{"test": "Hemoglobin A1c", "value": "8.9", "unit": "%"}]}))
        graph_span.set_attribute("langgraph.checkpoint.document_b64", pdf_b64)
        graph_span.set_attribute("langgraph.state.patient_uuid", puuid)
        graph_span.set_attribute("gen_ai.prompt.0.content", note)

    spans = _exported(exporter)
    assert any(s.name == "call_model" for s in spans), [s.name for s in spans]
    for s in spans:
        blob = json.dumps(dict(s.attributes), default=str)
        for needle in DOCUMENT_STRINGS:
            assert needle not in blob, (s.name, needle)
        assert puuid not in blob, s.name
        assert pdf_b64[:32] not in blob, s.name
        assert "JVBERi0" not in blob, s.name  # the base64 prefix of any PDF


# --------------------------------------------------------------------------- #
# Week 2: the document briefing is one trace (CR7)
# --------------------------------------------------------------------------- #


@pytest.fixture
def traced_document_client(exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from app.providers.stub_provider import StubProvider

    original = observability.configure_tracing
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    public_key = f"pk-lf-test-{uuid4().hex}"

    def configure_with_memory_exporter(**kwargs: Any) -> bool:
        kwargs.update(enabled=True, public_key=public_key, secret_key="sk-lf-test", base_url="http://127.0.0.1:9", span_exporter=exporter)
        return original(**kwargs)

    monkeypatch.setattr("app.main.configure_tracing", configure_with_memory_exporter)
    app.dependency_overrides[get_provider_factory] = lambda: StubProvider
    app.dependency_overrides[get_settings] = lambda: configured_settings()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_provider_factory, None)
        app.dependency_overrides.pop(get_settings, None)


_OPAQUE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{16,32}\b", re.IGNORECASE)


def test_document_briefing_is_one_trace_with_every_step_and_no_document_text(
    traced_document_client: TestClient, exporter: InMemorySpanExporter
) -> None:
    from tests.test_document_briefing import _body, _signed

    body = _body()
    r = traced_document_client.post("/v1/documents/briefing", content=body, headers=_signed(body))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    payload = json.loads(body)

    spans = _exported(exporter)
    by_name = {s.name: s for s in spans}
    # Tool sequence: every step of the pipeline is an observation in the trace.
    assert {"document_briefing", "supervisor", "intake-extractor", "evidence-retriever", "answer"} <= set(by_name)
    assert {"lab_extract", "retrieval.hybrid", "rerank", "answer_considerations"} <= set(by_name)
    assert {format(s.context.trace_id, "032x") for s in spans} == {trace_id_for(payload["correlation_id"])}
    root = by_name["document_briefing"]

    def parent_of(name: str) -> int:
        return by_name[name].parent.span_id

    # CR4: the supervisor and both workers sit directly under the encounter...
    for node in ("intake-extractor", "evidence-retriever", "answer"):
        assert parent_of(node) == root.context.span_id, node
    decisions = [s for s in spans if s.name == "supervisor"]
    assert len(decisions) == 4 and all(s.parent.span_id == root.context.span_id for s in decisions)
    # ...and each tool call sits under the worker that made it.
    assert parent_of("lab_extract") == by_name["intake-extractor"].context.span_id
    assert parent_of("retrieval.hybrid") == by_name["evidence-retriever"].context.span_id
    assert parent_of("rerank") == by_name["evidence-retriever"].context.span_id
    assert parent_of("answer_considerations") == by_name["answer"].context.span_id
    # Each handoff's reason code survives the mask.
    reasons = [
        next(v for k, v in s.attributes.items() if k.endswith("metadata.reason_code"))
        for s in sorted(decisions, key=lambda s: s.start_time)
    ]
    assert reasons == ["document_pending_extraction", "evidence_required", "evidence_ready", "briefing_complete"]

    # Both model calls are generations with usage and cost.
    for gen in ("lab_extract", "answer_considerations"):
        assert _attr_json(by_name[gen], "langfuse.observation.type") == "generation", gen
        assert "total" in _attr_json(by_name[gen], "langfuse.observation.cost_details"), gen

    root_meta = {k.rsplit(".", 1)[-1]: v for k, v in root.attributes.items() if "metadata" in k}
    assert root_meta.get("outcome") == "ok"

    # PHI control: no extracted value, test name, quote or identifier leaves the process.
    briefing = r.json()["briefing"]
    needles = {payload["patient_uuid"], payload["document_base64"][:40], "Hemoglobin A1c", r.json()["rendered_text"][:60]}
    for line in briefing["what_changed"] + briefing["needs_attention"]:
        needles.add(line["text"])
        if line["document_citation"]:
            needles.add(line["document_citation"]["quote_or_value"])
    assert "8.9" in needles  # the extracted lab value itself is one of the needles
    for s in spans:
        # Opaque ids (uuids, hex trace/span ids) are blanked first: a short value such as "164"
        # can occur inside a random id by chance, which made this test flaky. Ids are not PHI.
        blob = _OPAQUE_ID.sub("<id>", json.dumps(dict(s.attributes), default=str))
        for needle in needles:
            assert needle not in blob, (s.name, needle)


def test_intake_medication_conflicts_leave_no_drug_name_in_an_exported_span(
    traced_document_client: TestClient, exporter: InMemorySpanExporter
) -> None:
    """ADR-010: reported and chart medication names are compared in-process and never exported."""
    import asyncio

    from tests.test_document_briefing import _signed
    from tests.test_intake_briefing import _chart_med, _intake, _stored_intake
    from tests.test_stored_documents import _payload

    extraction = asyncio.run(_intake(301))
    chart = [_chart_med("prescriptions:11", "Metformin 1000 mg", "twice daily"), _chart_med("lists:12", "Zolpidemix 5 mg", "nightly")]
    payload = _payload([_stored_intake(301, extraction)], chart_medications=chart)
    body = json.dumps(payload).encode()
    r = traced_document_client.post("/v1/documents/briefing", content=body, headers=_signed(body))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    attention = r.json()["briefing"]["needs_attention"]
    assert any(line["text"].startswith("Patient reports") for line in attention)

    spans = _exported(exporter)
    assert any(s.name == "document_briefing" for s in spans)
    needles = {"Metformin", "Lisinopril", "Atorvastatin", "Zolpidemix", "twice daily", payload["patient_uuid"]}
    needles |= {line["text"] for line in attention}
    for s in spans:
        blob = json.dumps(dict(s.attributes), default=str)
        for needle in needles:
            assert needle not in blob, (s.name, needle)
