"""Phase 5 hardening: metrics and cost accounting, stub provider, readiness probes, /metrics."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.contracts import CommitmentKind, MedicationAction
from app.extractor import ground_extraction
from app.metrics import Metrics, estimate_cost_usd, metrics
from app.providers.base import ModelProvider
from app.providers.stub_provider import StubProvider
from tests.conftest import configured_settings


def test_cost_estimate_uses_longest_model_prefix_and_cached_discount() -> None:
    assert estimate_cost_usd("claude-opus-5", 1_000_000, 0, 0) == 5.0
    assert estimate_cost_usd("claude-opus-5", 1_000_000, 1_000_000, 0) == 0.5
    assert estimate_cost_usd("claude-opus-5-20990101", 0, 0, 1_000_000) == 25.0
    assert estimate_cost_usd("unknown-model", 1000, 0, 1000) == 0.0
    assert estimate_cost_usd("m", 1000, 0, 0, {"m": (10.0, 1.0, 20.0)}) == 0.01


def test_metrics_snapshot_has_counters_percentiles_tokens_and_cost() -> None:
    m = Metrics()
    m.inc("briefings_completed")
    m.inc("evidence_states", state="matching_result_found")
    m.inc("evidence_states", state="matching_result_found")
    for ms in (100, 200, 300, 400, 5000):
        m.observe("briefing", ms)
    cost = m.add_usage("claude-opus-5", 4000, 3000, 500)
    snap = m.snapshot()
    assert snap["counters"] == {"briefings_completed": 1, "evidence_states{state=matching_result_found}": 2}
    assert snap["latency"]["briefing"]["count"] == 5 and snap["latency"]["briefing"]["p50_ms"] == 300 and snap["latency"]["briefing"]["max_ms"] == 5000
    assert snap["tokens"] == {"input": 4000, "cached_input": 3000, "output": 500, "requests": 1}
    assert snap["estimated_cost_usd"] == round(cost, 6) == round((1000 * 5.0 + 3000 * 0.5 + 500 * 25.0) / 1_000_000, 6)


@pytest.mark.anyio
async def test_stub_provider_extracts_verbatim_spans_and_refuses_turns() -> None:
    stub = StubProvider()
    assert isinstance(stub, ModelProvider)
    plan = "Continue metformin. Repeat HbA1c in three months. Refer to cardiology."
    result = await stub.extract_commitments(plan)
    grounded = ground_extraction(plan, result.output)
    kinds = [(c.kind, c.source_span) for c in grounded.commitments]
    assert (CommitmentKind.MEDICATION, "Continue metformin.") in kinds
    assert (CommitmentKind.LAB_TEST, "Repeat HbA1c in three months.") in kinds
    assert grounded.commitments[0].action is MedicationAction.CONTINUE
    assert grounded.warnings == []  # every span is verbatim
    step = await stub.turn_step("sys", [], [], force_answer=False)
    assert step.answer is not None and step.answer.statements[0].kind == "refusal"
    assert await stub.ping() is True


def test_metrics_endpoint_and_stream_accounting(client: TestClient, fixture_payload: dict) -> None:
    from tests.test_handoff import post_bundle, read_events, ticket_for

    metrics.reset()
    accepted = post_bundle(client, fixture_payload)
    assert read_events(client, accepted["bundle_id"], ticket_for(accepted))[0] == 200
    snap = client.get("/metrics").json()
    assert snap["counters"]["briefings_started"] == 1 and snap["counters"]["briefings_completed"] == 1
    assert snap["counters"]["evidence_states{state=matching_result_found}"] == 1
    assert snap["latency"]["briefing"]["count"] == 1
    assert snap["tokens"]["requests"] == 1  # the fake provider reports usage too
    assert "8.9" not in str(snap) and "metformin" not in str(snap).lower()


def test_ready_reports_stub_provider_as_degraded_but_ready(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import _ready_cache

    _ready_cache.clear()
    monkeypatch.setenv("MODEL_PROVIDER", "stub")
    resp = client.get("/ready")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready" and body["dependencies"]["model_provider"]["status"] == "degraded"
    _ready_cache.clear()


@pytest.mark.parametrize("service_settings", [configured_settings(openemr_base_url="http://127.0.0.1:9", langfuse_host="http://127.0.0.1:9", ready_probe_timeout_seconds=0.5, ready_cache_seconds=0)])
def test_ready_probes_optional_dependencies_and_reports_unavailable(client: TestClient) -> None:
    from app.main import _ready_cache

    _ready_cache.clear()
    body = client.get("/ready").json()
    assert body["dependencies"]["openemr"]["status"] == "unavailable"
    assert body["dependencies"]["langfuse"]["status"] == "unavailable"
    assert body["status"] == "not_ready"
    _ready_cache.clear()
