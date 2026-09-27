"""Follow-ups see documents read on this chart open (``POST /v1/bundles/{id}/refresh``).

The bundle is built when the panel asks for its ticket - before the chart-open
processing loop has read new documents. When the loop reads one (or a value is
filed, rejected or un-filed), the module re-reads the pending facts and lab
results and replaces them in the bundle the agent already holds:

- signed by the module like ``POST /v1/bundles``; nothing from the browser;
- bound to the stored bundle: patient uuid, correlation id, user uuid, and
  (with lab results) the baseline note id must all match, else nothing changes;
- replaces only ``pending_document_facts`` and optionally ``lab_results``: the
  bundle id, its expiry, the verified plan check and the conversation stay;
- runs no model and no briefing.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.fakes import FakeProvider, answer, calls, hba1c, metformin, model_output, statement
from tests.test_followup import turn
from tests.test_handoff import USER_UUID, bearer, post_bundle, read_events, signed_headers, ticket_for

FACT = "copilot_extracted_value:77"


def fact(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "fact_id": FACT,
        "document_id": 930,
        "test_name": "Hemoglobin A1c",
        "value_text": "9.1",
        "unit": "%",
        "reference_range": "4.0-5.6",
        "abnormal_flag": None,
        "flag_source": "unavailable",
        "collection_date": "2026-09-24",
        "verification_status": "verified_exact",
        "page": 1,
        "bbox": None,
        "status": "candidate",
        "received_at": "2026-09-26",
    }
    row.update(over)
    return row


def refresh_body(accepted: dict[str, Any], **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "correlation_id": accepted["correlation_id"],
        "patient_uuid": accepted["patient_uuid"],
        "user_uuid": USER_UUID,
        "pending_document_facts": [fact()],
    }
    body.update(over)
    return body


def refresh(client: TestClient, bundle_id: str, body: dict[str, Any], *, sign: bool = True) -> Any:
    raw = json.dumps(body).encode("utf-8")
    headers = signed_headers(raw) if sign else {"Content-Type": "application/json"}
    return client.post(f"/v1/bundles/{bundle_id}/refresh", content=raw, headers=headers)


def stored(bundle_id: str) -> Any:
    from uuid import UUID

    return app.state.store._bundles[UUID(bundle_id)]


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider(
        model_output(metformin(), hba1c()),
        turn_script=[
            calls(("find_pending_document_facts", {"test_query": "hba1c"})),
            answer(statement("Hemoglobin A1c 9.1 % on 2026-09-24 from document 930 is not yet verified or filed.", "fact", FACT)),
        ],
    )


def test_refresh_replaces_pending_facts_and_keeps_the_plan_check_and_conversation(client: TestClient, fixture_payload: dict, fake_provider: FakeProvider) -> None:
    accepted = post_bundle(client, fixture_payload, user_uuid=USER_UUID)
    assert read_events(client, accepted["bundle_id"], ticket_for(accepted))[0] == 200
    before = stored(accepted["bundle_id"])
    matches, expires = before.matches, before.expires_at
    model_calls = len(fake_provider.calls)
    assert before.bundle.pending_document_facts == []

    resp = refresh(client, accepted["bundle_id"], refresh_body(accepted))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"bundle_id": accepted["bundle_id"], "correlation_id": accepted["correlation_id"], "patient_uuid": accepted["patient_uuid"], "pending_facts": 1, "lab_results": None}

    after = stored(accepted["bundle_id"])
    assert [f.fact_id for f in after.bundle.pending_document_facts] == [FACT]
    assert after.matches is matches and after.expires_at == expires, "the plan check and the bundle's lifetime stay"
    assert len(fake_provider.calls) == model_calls, "no model call: the briefing is not re-run"
    assert [r.result_id for r in after.bundle.lab_results] == [r.result_id for r in before.bundle.lab_results]

    # The next follow-up answers from the new fact.
    resp = turn(client, accepted["bundle_id"], ticket_for(accepted), "Any new A1c?")
    assert resp.status_code == 200, resp.text
    assert [c["record_id"] for s in resp.json()["statements"] for c in s["citations"]] == [FACT]


def test_refresh_can_replace_lab_results_for_the_same_baseline_note(client: TestClient, fixture_payload: dict) -> None:
    accepted = post_bundle(client, fixture_payload, user_uuid=USER_UUID)
    lab = dict(fixture_payload["context"]["lab_results"][0], result_id="procedure_result:9100", value="9.1", observed_at="2026-09-24T09:00:00Z")
    resp = refresh(client, accepted["bundle_id"], refresh_body(accepted, pending_document_facts=[], prior_note_id="form_soap:1001", lab_results=[lab]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["lab_results"] == 1
    after = stored(accepted["bundle_id"]).bundle
    assert [r.result_id for r in after.lab_results] == ["procedure_result:9100"]
    assert after.pending_document_facts == []


def test_lab_results_for_another_baseline_note_change_nothing(client: TestClient, fixture_payload: dict) -> None:
    accepted = post_bundle(client, fixture_payload, user_uuid=USER_UUID)
    resp = refresh(client, accepted["bundle_id"], refresh_body(accepted, prior_note_id="form_soap:9999", lab_results=[]))
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "baseline_note_changed"
    resp = refresh(client, accepted["bundle_id"], refresh_body(accepted, lab_results=[]))
    assert resp.status_code == 422, "lab results without the note they are windowed from are refused"
    assert stored(accepted["bundle_id"]).bundle.pending_document_facts == []


@pytest.mark.parametrize(
    ("field", "code"),
    [("patient_uuid", "patient_mismatch"), ("correlation_id", "correlation_mismatch"), ("user_uuid", "user_mismatch")],
)
def test_refresh_is_bound_to_the_stored_bundle(client: TestClient, fixture_payload: dict, field: str, code: str) -> None:
    accepted = post_bundle(client, fixture_payload, user_uuid=USER_UUID)
    resp = refresh(client, accepted["bundle_id"], refresh_body(accepted, **{field: str(uuid4())}))
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == code
    assert stored(accepted["bundle_id"]).bundle.pending_document_facts == []


def test_refresh_requires_the_module_signature_and_a_live_bundle(client: TestClient, fixture_payload: dict) -> None:
    accepted = post_bundle(client, fixture_payload, user_uuid=USER_UUID)
    assert refresh(client, accepted["bundle_id"], refresh_body(accepted), sign=False).status_code == 401
    assert stored(accepted["bundle_id"]).bundle.pending_document_facts == []
    missing = refresh(client, str(uuid4()), refresh_body(accepted))
    assert missing.status_code == 404
    client.delete(f"/v1/bundles/{accepted['bundle_id']}", headers=bearer(ticket_for(accepted)))
    assert refresh(client, accepted["bundle_id"], refresh_body(accepted)).status_code == 404


def test_refresh_validates_facts_like_a_bundle(client: TestClient, fixture_payload: dict) -> None:
    accepted = post_bundle(client, fixture_payload, user_uuid=USER_UUID)
    resp = refresh(client, accepted["bundle_id"], refresh_body(accepted, pending_document_facts=[fact(status="filed")]))
    assert resp.status_code == 422
    assert "9.1" not in resp.text, "no value echoed back"
    too_many = [fact(fact_id=f"copilot_extracted_value:{i}") for i in range(1, 502)]
    assert refresh(client, accepted["bundle_id"], refresh_body(accepted, pending_document_facts=too_many)).status_code == 422
