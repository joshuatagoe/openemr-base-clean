"""Langfuse tracing: the PHI allow-list mask and the trace shape (ARCHITECTURE.md section 14).

The end-to-end tests run the real Langfuse SDK with an in-memory OpenTelemetry
exporter, so what is asserted on is exactly what would have been sent.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
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
