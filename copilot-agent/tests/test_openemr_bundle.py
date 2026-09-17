"""Contract compatibility between the OpenEMR adapter and the agent.

``fixtures/openemr_evelyn_demo_context.json`` was captured from
``GET /apis/default/api/copilot/context`` on the local development stack for
the synthetic patient Evelyn Demo (seeded by
interface/modules/custom_modules/oe-module-copilot/dev/seed_evelyn_demo.php).
These tests guard against the PHP builder and the Python contracts drifting
apart. No model call is made (scripted fake provider).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.contracts import BriefingRequest, EvidenceState, LabResultStatus
from tests.fakes import hba1c, metformin, model_output

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "openemr_evelyn_demo_context.json"


@pytest.fixture
def openemr_payload() -> dict:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_openemr_bundle_validates_against_contract(openemr_payload: dict) -> None:
    """Regression guard: the adapter's output is a valid BriefingRequest with stable OpenEMR record ids and UTC times."""
    req = BriefingRequest.model_validate(openemr_payload)
    ctx = req.context
    assert ctx.schema_version == "1.0"
    assert ctx.prior_note.note_id.startswith("form_soap:")
    assert ctx.prior_note.encounter_id.startswith("form_encounter:")
    assert ctx.prior_note.note_date.utcoffset().total_seconds() == 0
    assert ctx.data_quality.sources_unavailable == []
    assert len(ctx.lab_results) == 1
    r = ctx.lab_results[0]
    assert r.result_id.startswith("procedure_result:")
    assert r.status is LabResultStatus.FINAL
    assert str(r.value) == "8.9" and r.units == "%"
    assert r.observed_at > ctx.prior_note.note_date


def test_openemr_bundle_contains_no_direct_identifiers(openemr_payload: dict) -> None:
    """Invariant: the adapter sends no name, DOB, address or phone - only the patient uuid."""
    text = json.dumps(openemr_payload).lower()
    for forbidden in ("evelyn", "demo", "dob", "1958", "phone", "street", "address", "fname", "lname"):
        assert forbidden not in text, forbidden


@pytest.mark.parametrize("provider_script", [[model_output(metformin(), hba1c())]])
def test_openemr_bundle_yields_matching_result_found_end_to_end(client: TestClient, openemr_payload: dict) -> None:
    """Tracer bullet on real adapter output: the seeded final HbA1c is matched and cited."""
    resp = client.post("/v1/briefings", json=openemr_payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    lab = next(m for m in body["matches"] if m["commitment"]["kind"] == "lab_test")
    assert lab["state"] == EvidenceState.MATCHING_RESULT_FOUND.value
    cited = {c["record_id"] for c in lab["citations"]}
    assert cited == {openemr_payload["context"]["prior_note"]["note_id"], openemr_payload["context"]["lab_results"][0]["result_id"]}
    assert "8.9%" in lab["summary"] and "high" in lab["summary"]
