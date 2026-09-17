"""API and contract tests for the agent service.

The ``client`` and ``fixture_payload`` fixtures come from ``conftest.py``; the
client's model provider is a scripted fake, so no test here reaches the network.

Every test names the failure mode it guards against (AgentForge engineering
requirement: boundary, invariant or regression - no happy-path-only suites).
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.contracts import (
    BriefingRequest,
    Citation,
    CommitmentKind,
    EvidenceMatch,
    EvidenceState,
    ExtractedCommitment,
    RecordType,
)
from app.main import CORRELATION_HEADER

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "lab_followup.json"


# --------------------------------------------------------------------------- #
# Liveness
# --------------------------------------------------------------------------- #


def test_health_returns_ok(client: TestClient) -> None:
    """Guards: the process answers liveness with the exact documented body."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# Fixture acceptance and identifier echo
# --------------------------------------------------------------------------- #


def test_fixture_conforms_to_briefing_request(fixture_payload: dict) -> None:
    """Invariant: the committed fixture is a valid BriefingRequest (regression guard for contract drift)."""
    parsed = BriefingRequest.model_validate(fixture_payload)
    assert parsed.context.prior_note.plan_text == "Continue metformin. Repeat HbA1c in three months."
    assert len(parsed.context.lab_results) == 1
    assert str(parsed.context.lab_results[0].value) == "8.9"


def test_briefing_accepts_fixture_and_echoes_identifiers(client: TestClient, fixture_payload: dict) -> None:
    """Invariant: the response is bound to the same correlation id and patient as the request (ARCH-002)."""
    resp = client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["correlation_id"] == fixture_payload["context"]["correlation_id"]
    assert body["patient_uuid"] == fixture_payload["context"]["patient_uuid"]
    assert resp.headers[CORRELATION_HEADER] == fixture_payload["context"]["correlation_id"]


def test_briefing_returns_matches_for_the_fixture(client: TestClient, fixture_payload: dict) -> None:
    """Regression guard: the wired endpoint evaluates the fixture (full scenarios live in test_briefing.py)."""
    body = client.post("/v1/briefings", json=fixture_payload).json()
    assert len(body["matches"]) == 2
    assert not any("not wired" in w for w in body["warnings"])


# --------------------------------------------------------------------------- #
# Boundary: strictness
# --------------------------------------------------------------------------- #


def test_unexpected_field_is_rejected(client: TestClient, fixture_payload: dict) -> None:
    """Boundary: extra="forbid" - an unknown field (here a direct identifier) is a 422, never accepted."""
    fixture_payload["context"]["patient_name"] = "should not be here"
    resp = client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 422
    locs = [tuple(e["loc"]) for e in resp.json()["detail"]]
    assert ("body", "context", "patient_name") in locs


def test_validation_errors_do_not_echo_input(client: TestClient, fixture_payload: dict) -> None:
    """Guards: 422 bodies carry location/message/type only, never the submitted clinical text."""
    fixture_payload["context"]["prior_note"]["plan_text"] = ""
    resp = client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 422
    for err in resp.json()["detail"]:
        assert set(err) == {"loc", "msg", "type"}


def test_invalid_enum_value_is_rejected_by_api(client: TestClient, fixture_payload: dict) -> None:
    """Boundary: a lab abnormal flag outside the closed set is rejected (interpretation must come from source flags)."""
    fixture_payload["context"]["lab_results"][0]["abnormal_flag"] = "critical"
    resp = client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 422


def test_invalid_evidence_state_is_rejected_by_pydantic() -> None:
    """Boundary: there is no generic 'done' state; only the enumerated source-specific states validate."""
    commitment = ExtractedCommitment(
        commitment_id="c1",
        kind=CommitmentKind.LAB_TEST,
        source_span="Repeat HbA1c in three months.",
        test_name="Hemoglobin A1c",
        due_text="in three months",
    )
    with pytest.raises(ValidationError):
        EvidenceMatch.model_validate(
            {"commitment": commitment.model_dump(), "state": "done", "summary": "x", "citations": []}
        )


def test_found_state_without_citation_is_rejected() -> None:
    """Invariant: a state that asserts a record exists must cite it (ARCHITECTURE.md section 9)."""
    commitment = ExtractedCommitment(
        commitment_id="c1", kind=CommitmentKind.LAB_TEST, source_span="Repeat HbA1c in three months."
    )
    with pytest.raises(ValidationError):
        EvidenceMatch(
            commitment=commitment,
            state=EvidenceState.MATCHING_RESULT_FOUND,
            summary="HbA1c result on file",
            citations=[],
        )
    # The same state with a citation is valid.
    ok = EvidenceMatch(
        commitment=commitment,
        state=EvidenceState.MATCHING_RESULT_FOUND,
        summary="HbA1c result on file",
        citations=[
            Citation(
                record_type=RecordType.LAB_RESULT,
                record_id="procedure_result:9001",
                timestamp=datetime(2026, 9, 12, 9, 15, tzinfo=timezone.utc),
            )
        ],
    )
    assert ok.state is EvidenceState.MATCHING_RESULT_FOUND


def test_no_matching_record_found_is_valid_without_citations() -> None:
    """Boundary: 'no matching record found' is a scoped absence claim, not a record claim - no citation required."""
    commitment = ExtractedCommitment(
        commitment_id="c1", kind=CommitmentKind.LAB_TEST, source_span="Repeat HbA1c in three months."
    )
    match = EvidenceMatch(
        commitment=commitment,
        state=EvidenceState.NO_MATCHING_RECORD_FOUND,
        summary="No HbA1c order or result found in this system after 2026-06-10.",
    )
    assert match.citations == []


def test_missing_required_prior_note_field_is_rejected(client: TestClient, fixture_payload: dict) -> None:
    """Boundary: a prior note without plan text cannot be evaluated and must fail validation, not silently pass."""
    del fixture_payload["context"]["prior_note"]["plan_text"]
    resp = client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 422
    locs = [tuple(e["loc"]) for e in resp.json()["detail"]]
    assert ("body", "context", "prior_note", "plan_text") in locs


def test_missing_prior_note_entirely_is_rejected(client: TestClient, fixture_payload: dict) -> None:
    """Boundary: the bundle must carry a baseline note; the scaffold does not invent one."""
    del fixture_payload["context"]["prior_note"]
    resp = client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 422


def test_naive_timestamp_is_rejected(client: TestClient, fixture_payload: dict) -> None:
    """Boundary: timestamps must be timezone-aware so evidence windows are unambiguous."""
    fixture_payload["context"]["lab_results"][0]["observed_at"] = "2026-09-12T09:15:00"
    resp = client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 422


def test_unknown_schema_version_is_rejected(client: TestClient, fixture_payload: dict) -> None:
    """Boundary: an unknown contract version is refused rather than guessed (ARCHITECTURE.md section 7)."""
    fixture_payload["context"]["schema_version"] = "2.0"
    resp = client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 422


def test_openapi_schema_is_generated(client: TestClient) -> None:
    """Regression guard: the contracts render to OpenAPI without error and expose both routes."""
    schema = client.get("/openapi.json").json()
    assert "/health" in schema["paths"]
    assert "/v1/briefings" in schema["paths"]
    assert "BriefingRequest" in schema["components"]["schemas"]
    assert "EvidenceState" in schema["components"]["schemas"]


def test_fixture_is_unchanged_by_tests(fixture_payload: dict) -> None:
    """Regression guard: per-test copies mean mutations never reach the committed fixture."""
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert copy.deepcopy(fixture_payload) == on_disk
