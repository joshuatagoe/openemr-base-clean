"""End-to-end briefing tests: POST /v1/briefings and BriefingService.

The model provider is always a scripted fake (see conftest.py). Each test
names the failure mode it guards against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.contracts import BriefingRequest, EvidenceSource, EvidenceState
from app.extractor import MODEL_NOTES_WARNING, REJECTED_SPAN_WARNING
from app.main import CORRELATION_HEADER
from app.providers.base import (
    MalformedModelOutputError,
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.service import NO_USABLE_PLAN_WARNING, PLAN_TOO_LONG_WARNING, BriefingService, select_plan_text
from tests.fakes import FakeProvider, hba1c, metformin, model_output

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "lab_followup.json"
CID = "7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b"
PUUID = "3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e"


def by_kind(body: dict) -> dict[str, dict]:
    return {m["commitment"]["kind"]: m for m in body["matches"]}


# --------------------------------------------------------------------------- #
# Complete scenario: HbA1c follow-up commitment + later final HbA1c result
# --------------------------------------------------------------------------- #


def test_full_scenario_matching_result_found(client: TestClient, fixture_payload: dict, fake_provider: FakeProvider) -> None:
    """Tracer bullet end to end: commitment, evidence state, neutral summary, and source citations."""
    resp = client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["correlation_id"] == CID and body["patient_uuid"] == PUUID
    assert resp.headers[CORRELATION_HEADER] == CID
    assert fake_provider.calls == ["Continue metformin. Repeat HbA1c in three months."]  # plan text only

    lab = by_kind(body)["lab_test"]
    assert lab["commitment"]["source_span"] == "Repeat HbA1c in three months."
    assert lab["commitment"]["test_name"] == "HbA1c"
    assert lab["commitment"]["due_text"] == "in three months"
    assert lab["state"] == EvidenceState.MATCHING_RESULT_FOUND.value
    assert "8.9%" in lab["summary"] and "high" in lab["summary"]
    for forbidden in ("should", "recommend", "increase", "adjust", "uncontrolled"):
        assert forbidden not in lab["summary"].lower()
    cited = {(c["record_type"], c["record_id"], c["timestamp"]) for c in lab["citations"]}
    assert cited == {
        ("prior_note", "form_soap:1001", "2026-06-10T14:30:00Z"),
        ("lab_result", "procedure_result:9001", "2026-09-12T09:15:00Z"),
    }
    assert body["warnings"] == []


def test_ids_are_result_scoped_in_source_order(client: TestClient, fixture_payload: dict) -> None:
    body = client.post("/v1/briefings", json=fixture_payload).json()
    assert [m["commitment"]["commitment_id"] for m in body["matches"]] == ["c-001", "c-002"]
    assert [m["commitment"]["kind"] for m in body["matches"]] == ["medication", "lab_test"]


# --------------------------------------------------------------------------- #
# Explicit cases
# --------------------------------------------------------------------------- #


def test_no_matching_result(client: TestClient, fixture_payload: dict) -> None:
    """Boundary: no subsequent result -> scoped absence claim citing only the note; never 'not done'."""
    fixture_payload["context"]["lab_results"] = []
    body = client.post("/v1/briefings", json=fixture_payload).json()
    lab = by_kind(body)["lab_test"]
    assert lab["state"] == EvidenceState.NO_MATCHING_RECORD_FOUND.value
    assert [c["record_type"] for c in lab["citations"]] == ["prior_note"]
    assert "supplied records" in lab["summary"]


def test_unsupported_commitment_type_is_verification_unavailable(client: TestClient, fixture_payload: dict) -> None:
    """Boundary: a medication commitment is carried but reported as not evaluated, not as absent evidence."""
    body = client.post("/v1/briefings", json=fixture_payload).json()
    med = by_kind(body)["medication"]
    assert med["commitment"]["source_span"] == "Continue metformin."
    assert med["state"] == EvidenceState.VERIFICATION_UNAVAILABLE.value
    assert "not supported" in med["summary"]


def test_unavailable_lab_source_is_verification_unavailable(client: TestClient, fixture_payload: dict) -> None:
    """Guards: 'could not check' never collapses into 'nothing found' across the whole pipeline."""
    fixture_payload["context"]["data_quality"]["sources_unavailable"] = [EvidenceSource.LAB_RESULTS.value]
    body = client.post("/v1/briefings", json=fixture_payload).json()
    assert by_kind(body)["lab_test"]["state"] == EvidenceState.VERIFICATION_UNAVAILABLE.value


@pytest.mark.parametrize("plan_text", ["---", "...", "***"])
def test_no_usable_prior_plan_skips_the_model(client: TestClient, fixture_payload: dict, fake_provider: FakeProvider, plan_text: str) -> None:
    """Boundary: a plan with no letters or digits is reported as unusable and the model is never called."""
    fixture_payload["context"]["prior_note"]["plan_text"] = plan_text
    resp = client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 200
    assert resp.json()["matches"] == []
    assert resp.json()["warnings"] == [NO_USABLE_PLAN_WARNING]
    assert fake_provider.calls == []


@pytest.mark.parametrize(
    "error, status_code, code",
    [
        (ProviderUnavailableError("u"), 503, "provider_unavailable"),
        (ProviderRateLimitError("r"), 503, "provider_rate_limited"),
        (ProviderTimeoutError("t"), 504, "provider_timeout"),
        (ProviderAuthenticationError("a"), 503, "provider_authentication_failed"),
        (MalformedModelOutputError("m"), 502, "malformed_model_output"),
    ],
)
def test_provider_failure_is_an_explicit_error_not_an_empty_briefing(
    fixture_payload: dict, error: Exception, status_code: int, code: str
) -> None:
    """Guards: an outage or bad output is a structured error with identifiers, never a 200 with no matches."""
    from app.main import app, get_provider_factory

    provider = FakeProvider(error)
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/briefings", json=fixture_payload)
    finally:
        app.dependency_overrides.pop(get_provider_factory, None)

    assert resp.status_code == status_code
    detail = resp.json()["detail"]
    assert detail["code"] == code
    assert detail["correlation_id"] == CID and detail["patient_uuid"] == PUUID
    assert resp.headers[CORRELATION_HEADER] == CID
    assert "matches" not in resp.json()
    assert "metformin" not in resp.text and "HbA1c" not in resp.text  # no clinical content in error bodies
    expected_calls = 2 if getattr(error, "retryable", False) else 1  # bounded retry
    assert len(provider.calls) == expected_calls


def test_rate_limit_error_carries_retry_after(fixture_payload: dict) -> None:
    from app.main import app, get_provider_factory

    app.dependency_overrides[get_provider_factory] = lambda: (lambda: FakeProvider(ProviderRateLimitError("r")))
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/briefings", json=fixture_payload)
    finally:
        app.dependency_overrides.pop(get_provider_factory, None)
    assert resp.status_code == 503 and resp.headers["Retry-After"] == "5"


def test_provider_not_configured_returns_503(unconfigured_client: TestClient, fixture_payload: dict) -> None:
    """Guards: with no API key the real provider cannot be built; the request fails explicitly and nothing is sent."""
    resp = unconfigured_client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "provider_not_configured"
    assert resp.json()["detail"]["correlation_id"] == CID


def test_health_does_not_require_provider(unconfigured_client: TestClient) -> None:
    assert unconfigured_client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("provider_script", [[model_output(hba1c(source_span="Repeat A1c in 3 months."))]])
def test_ungrounded_extraction_yields_no_matches_and_a_generic_warning(client: TestClient, fixture_payload: dict) -> None:
    """Guards: a hallucinated span produces no match and a fixed warning that carries no clinical text."""
    body = client.post("/v1/briefings", json=fixture_payload).json()
    assert body["matches"] == []
    assert body["warnings"] == [REJECTED_SPAN_WARNING]
    assert "A1c" not in " ".join(body["warnings"])


@pytest.mark.parametrize("provider_script", [[model_output(hba1c(), warnings=["Patient likely non-adherent"])]])
def test_model_authored_warnings_are_not_shown(client: TestClient, fixture_payload: dict) -> None:
    """Guards: model prose never reaches the user; only its count is reported."""
    body = client.post("/v1/briefings", json=fixture_payload).json()
    assert body["warnings"] == [MODEL_NOTES_WARNING.format(n=1)]
    assert "non-adherent" not in json.dumps(body)


# --------------------------------------------------------------------------- #
# Service-level
# --------------------------------------------------------------------------- #


def load_request() -> BriefingRequest:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return BriefingRequest.model_validate(json.load(fh))


def test_select_plan_text_rules() -> None:
    request = load_request()
    assert select_plan_text(request.context) == ("Continue metformin. Repeat HbA1c in three months.", [])
    long_ctx = request.context.model_copy(update={"prior_note": request.context.prior_note.model_copy(update={"plan_text": "x" * 21})}, deep=True)
    assert select_plan_text(long_ctx, max_chars=20) == (None, [PLAN_TOO_LONG_WARNING])
    blank_ctx = request.context.model_copy(update={"prior_note": request.context.prior_note.model_copy(update={"plan_text": "- - -"})}, deep=True)
    assert select_plan_text(blank_ctx) == (None, [NO_USABLE_PLAN_WARNING])


@pytest.mark.anyio
async def test_service_constructs_provider_lazily_per_briefing() -> None:
    """Guards: building the service never builds a provider; each briefing gets a fresh provider from the factory."""
    built: list[FakeProvider] = []

    def factory() -> FakeProvider:
        provider = FakeProvider(model_output(hba1c()))
        built.append(provider)
        return provider

    service = BriefingService(factory)
    assert built == []
    request = load_request()
    first = await service.build_briefing(request)
    second = await service.build_briefing(request)
    assert len(built) == 2
    assert first == second
    assert first.matches[0].state is EvidenceState.MATCHING_RESULT_FOUND


@pytest.mark.anyio
async def test_service_does_not_mutate_request() -> None:
    request = load_request()
    before = request.model_dump()
    await BriefingService(lambda: FakeProvider(model_output(hba1c()))).build_briefing(request)
    assert request.model_dump() == before
