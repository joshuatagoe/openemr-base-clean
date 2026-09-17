"""Hand-off surface: signed bundle posts, ticket-gated briefing stream, deletion, readiness.

These are the Phase 0 trust-boundary tests (ARCHITECTURE.md sections 5, 10
and 15 "Patient isolation"): a ticket for bundle A used on B, an expired or
replayed ``jti``, a tampered patient uuid, and a stale event after delete must
all fail closed. The model provider is the scripted fake from conftest.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.contracts import BriefingRequest, EvidenceState
from app.main import CORRELATION_HEADER, app, get_provider_factory
from app.providers.base import ProviderUnavailableError
from app.security import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ExpiredTicketError,
    InvalidSignatureError,
    InvalidTicketError,
    StaleSignatureError,
    TicketClaims,
    mint_ticket,
    sign_body,
    verify_body_signature,
    verify_ticket,
)
from tests.conftest import TEST_TICKET_SECRET, configured_settings
from tests.fakes import FakeProvider, hba1c, metformin, model_output

USER_UUID = "9c3f0a2b-1d4e-4f5a-8b6c-7d8e9f0a1b2c"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def bundle_bytes(fixture_payload: dict, **overrides: Any) -> bytes:
    context = dict(fixture_payload["context"])
    context.update(overrides)
    return json.dumps(context).encode("utf-8")


def signed_headers(body: bytes, *, secret: str = TEST_TICKET_SECRET, timestamp: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if timestamp is None else timestamp
    return {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: str(ts),
        SIGNATURE_HEADER: sign_body(secret, body, ts),
    }


def post_bundle(client: TestClient, fixture_payload: dict, **overrides: Any) -> dict:
    body = bundle_bytes(fixture_payload, **overrides)
    resp = client.post("/v1/bundles", content=body, headers=signed_headers(body))
    assert resp.status_code == 201, resp.text
    return resp.json()


def ticket_for(accepted: dict, *, secret: str = TEST_TICKET_SECRET, ttl: int = 120, sub: str = USER_UUID, **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": sub,
        "puuid": accepted["patient_uuid"],
        "bundle_id": accepted["bundle_id"],
        "cid": accepted["correlation_id"],
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + ttl,
    }
    claims.update(overrides)
    return mint_ticket(secret, TicketClaims.model_validate(claims))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def read_events(client: TestClient, bundle_id: str, token: str) -> tuple[int, Any, list[tuple[str, dict]]]:
    """Open the SSE stream and return (status, headers, [(event, data), ...])."""
    with client.stream("GET", f"/v1/briefings/{bundle_id}", headers=bearer(token)) as resp:
        status = resp.status_code
        headers = resp.headers  # case-insensitive mapping
        raw = resp.read().decode("utf-8")
    if status != 200:
        return status, headers, [(f"http_{status}", json.loads(raw))]
    events: list[tuple[str, dict]] = []
    for frame in raw.strip().split("\n\n"):
        if not frame.strip():
            continue
        name = data = None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        assert name is not None and data is not None, frame
        events.append((name, data))
    return status, headers, events


# --------------------------------------------------------------------------- #
# Security primitives
# --------------------------------------------------------------------------- #


def test_body_signature_roundtrip_and_tamper_detection() -> None:
    """Invariant: a signature verifies only for the exact body, timestamp and secret it was made with."""
    body = b'{"a":1}'
    ts = 1_700_000_000
    sig = sign_body("s" * 32, body, ts)
    assert verify_body_signature("s" * 32, body, str(ts), sig, max_skew_seconds=60, now=ts + 10) == ts
    with pytest.raises(InvalidSignatureError):
        verify_body_signature("s" * 32, b'{"a":2}', str(ts), sig, max_skew_seconds=60, now=ts)
    with pytest.raises(InvalidSignatureError):
        verify_body_signature("x" * 32, body, str(ts), sig, max_skew_seconds=60, now=ts)
    with pytest.raises(InvalidSignatureError):
        verify_body_signature("s" * 32, body, str(ts + 1), sig, max_skew_seconds=60, now=ts)
    with pytest.raises(StaleSignatureError):
        verify_body_signature("s" * 32, body, str(ts), sig, max_skew_seconds=60, now=ts + 61)
    with pytest.raises(InvalidSignatureError):
        verify_body_signature("s" * 32, body, None, None, max_skew_seconds=60, now=ts)


def test_ticket_roundtrip_rejects_tampering_wrong_secret_and_algorithm_confusion() -> None:
    """Invariant: only an HS256 token signed with the shared secret yields claims; any edit invalidates it."""
    now = int(time.time())
    claims = TicketClaims(sub=USER_UUID, puuid=uuid4(), bundle_id=uuid4(), cid=uuid4(), jti=uuid4(), iat=now, exp=now + 60)
    token = mint_ticket("k" * 32, claims)
    assert verify_ticket("k" * 32, token, now=now + 1) == claims

    with pytest.raises(InvalidTicketError):
        verify_ticket("wrong-secret-wrong-secret-wrong-secret", token, now=now)
    with pytest.raises(ExpiredTicketError):
        verify_ticket("k" * 32, token, now=now + 60)
    assert verify_ticket("k" * 32, token, now=now + 60, check_expiry=False) == claims

    header, payload, signature = token.split(".")
    # Flip the patient uuid in the payload: the signature no longer covers it.
    tampered_payload = payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB")
    with pytest.raises(InvalidTicketError):
        verify_ticket("k" * 32, f"{header}.{tampered_payload}.{signature}", now=now)
    # alg=none style token with an empty signature is not a valid ticket.
    with pytest.raises(InvalidTicketError):
        verify_ticket("k" * 32, f"{header}.{payload}.", now=now)
    with pytest.raises(InvalidTicketError):
        verify_ticket("k" * 32, "not-a-token", now=now)


def test_ticket_claims_require_exp_after_iat() -> None:
    """Boundary: a ticket that expires at or before issue is malformed, not merely expired."""
    now = int(time.time())
    with pytest.raises(ValueError):
        TicketClaims(sub="u", puuid=uuid4(), bundle_id=uuid4(), cid=uuid4(), jti=uuid4(), iat=now, exp=now)


# --------------------------------------------------------------------------- #
# POST /v1/bundles
# --------------------------------------------------------------------------- #


def test_signed_bundle_is_stored_and_ids_are_echoed(client: TestClient, fixture_payload: dict) -> None:
    """Tracer bullet: the module's signed post yields a bundle id bound to the same cid and patient."""
    body = bundle_bytes(fixture_payload)
    resp = client.post("/v1/bundles", content=body, headers=signed_headers(body))
    assert resp.status_code == 201, resp.text
    accepted = resp.json()
    UUID(accepted["bundle_id"])
    assert accepted["correlation_id"] == fixture_payload["context"]["correlation_id"]
    assert accepted["patient_uuid"] == fixture_payload["context"]["patient_uuid"]
    assert resp.headers[CORRELATION_HEADER] == accepted["correlation_id"]
    assert accepted["expires_at"].endswith("Z") or "+00:00" in accepted["expires_at"]


@pytest.mark.parametrize(
    ("headers_fn", "expected_code"),
    [
        (lambda body: {"Content-Type": "application/json"}, "invalid_signature"),
        (lambda body: signed_headers(body, secret="another-secret-that-is-long-enough-123"), "invalid_signature"),
        (lambda body: signed_headers(body + b" "), "invalid_signature"),
        (lambda body: signed_headers(body, timestamp=int(time.time()) - 3600), "stale_signature"),
        (lambda body: {**signed_headers(body), TIMESTAMP_HEADER: "yesterday"}, "invalid_signature"),
    ],
    ids=["unsigned", "wrong-secret", "body-tampered", "stale-timestamp", "malformed-timestamp"],
)
def test_unsigned_or_tampered_bundle_is_rejected(client: TestClient, fixture_payload: dict, headers_fn, expected_code: str) -> None:
    """Guards: nothing reaches the store without the module's signature over the exact body."""
    body = bundle_bytes(fixture_payload)
    resp = client.post("/v1/bundles", content=body, headers=headers_fn(body))
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"]["code"] == expected_code
    assert "plan" not in resp.text.lower()


def test_bundle_post_without_secret_configured_is_503(unconfigured_client: TestClient, fixture_payload: dict) -> None:
    """Boundary: a service without the shared secret cannot accept bundles; it says so rather than accepting unsigned data."""
    body = bundle_bytes(fixture_payload)
    resp = unconfigured_client.post("/v1/bundles", content=body, headers=signed_headers(body))
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "not_configured"


def test_signed_but_invalid_bundle_is_422_without_echo(client: TestClient, fixture_payload: dict) -> None:
    """Boundary: signature first, then contract; validation errors never echo the submitted text."""
    body = bundle_bytes(fixture_payload, patient_name="should not be here")
    resp = client.post("/v1/bundles", content=body, headers=signed_headers(body))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert ("patient_name",) == tuple(detail[0]["loc"]) or "patient_name" in detail[0]["loc"]
    assert all(set(e) == {"loc", "msg", "type"} for e in detail)
    assert "should not be here" not in resp.text


def test_short_secret_counts_as_not_configured() -> None:
    """Boundary: a secret below the minimum length is treated as absent, never used."""
    assert configured_settings(ticket_secret="short").has_ticket_secret() is False
    assert configured_settings().has_ticket_secret() is True


# --------------------------------------------------------------------------- #
# GET /v1/briefings/{bundle_id} - the stream
# --------------------------------------------------------------------------- #


def test_briefing_stream_delivers_verified_commitments_bound_to_the_patient(client: TestClient, fixture_payload: dict) -> None:
    """Tracer bullet end to end: one extracted commitment, one matched result with a citation, streamed under a ticket."""
    accepted = post_bundle(client, fixture_payload)
    status, headers, events = read_events(client, accepted["bundle_id"], ticket_for(accepted))
    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert headers[CORRELATION_HEADER] == accepted["correlation_id"]
    assert headers["cache-control"] == "no-store, private"

    names = [name for name, _ in events]
    assert names == ["commitment", "commitment", "interval_annotation", "complete"], names
    for _, data in events:
        assert data["correlation_id"] == accepted["correlation_id"]
        assert data["patient_uuid"] == accepted["patient_uuid"]
    annotations = events[2][1]["annotations"]
    assert annotations == [{"record_id": "procedure_result:9001", "record_type": "lab_result", "explained_by": "c-002"}]

    lab = next(d["match"] for n, d in events if n == "commitment" and d["match"]["commitment"]["kind"] == "lab_test")
    assert lab["state"] == EvidenceState.MATCHING_RESULT_FOUND.value
    assert lab["commitment"]["source_span"] == "Repeat HbA1c in three months."
    assert {c["record_id"] for c in lab["citations"]} == {"form_soap:1001", "procedure_result:9001"}
    complete = events[-1][1]
    assert complete["commitments"] == 2 and complete["warnings"] == [] and complete["rejected_count"] == 0


@pytest.mark.parametrize("provider_script", [[model_output(metformin(), hba1c(source_span="All labs were completed."))]])
def test_withheld_proposals_are_counted_never_rendered(client: TestClient, fixture_payload: dict) -> None:
    """Invariant: a fabricated span is dropped; the panel learns only that one proposal was withheld."""
    accepted = post_bundle(client, fixture_payload)
    status, _, events = read_events(client, accepted["bundle_id"], ticket_for(accepted))
    assert status == 200
    complete = events[-1][1]
    assert complete["rejected_count"] == 1 and complete["commitments"] == 1
    assert "All labs were completed." not in json.dumps(events)


def test_ticket_is_single_use(client: TestClient, fixture_payload: dict) -> None:
    """Patient isolation: a replayed jti is refused even though the ticket is otherwise valid."""
    accepted = post_bundle(client, fixture_payload)
    token = ticket_for(accepted)
    assert read_events(client, accepted["bundle_id"], token)[0] == 200
    status, _, events = read_events(client, accepted["bundle_id"], token)
    assert status == 409 and events[0][1]["detail"]["code"] == "ticket_reused"
    # A fresh ticket for the same bundle still works: the bundle itself is not consumed.
    assert read_events(client, accepted["bundle_id"], ticket_for(accepted))[0] == 200


def test_ticket_for_bundle_a_cannot_read_bundle_b(client: TestClient, fixture_payload: dict) -> None:
    """Patient isolation (ARCH-002/SEC-002): two bundles, one ticket each; crossing them fails closed with no clinical content."""
    a = post_bundle(client, fixture_payload)
    b = post_bundle(client, fixture_payload, correlation_id=str(uuid4()), patient_uuid=str(uuid4()))
    token_a = ticket_for(a)

    status, _, events = read_events(client, b["bundle_id"], token_a)
    assert status == 403 and events[0][1]["detail"]["code"] == "ticket_bundle_mismatch"
    assert "HbA1c" not in json.dumps(events)

    # Ticket A on bundle A still works afterwards; B is untouched.
    status, _, events = read_events(client, a["bundle_id"], token_a)
    assert status == 200 and all(d["patient_uuid"] == a["patient_uuid"] for _, d in events)
    status, _, events = read_events(client, b["bundle_id"], ticket_for(b))
    assert status == 200 and all(d["patient_uuid"] == b["patient_uuid"] for _, d in events)


def test_ticket_naming_a_different_patient_for_the_same_bundle_is_refused(client: TestClient, fixture_payload: dict) -> None:
    """Fail closed when the ticket's puuid is changed: even a correctly signed ticket must match the stored bundle."""
    accepted = post_bundle(client, fixture_payload)
    token = ticket_for(accepted, puuid=str(uuid4()))
    status, _, events = read_events(client, accepted["bundle_id"], token)
    assert status == 403 and events[0][1]["detail"]["code"] == "ticket_patient_mismatch"


def test_ticket_with_wrong_correlation_id_is_refused(client: TestClient, fixture_payload: dict) -> None:
    """Fail closed: the ticket's cid must be the bundle's cid, so a trace is never split across briefings."""
    accepted = post_bundle(client, fixture_payload)
    status, _, events = read_events(client, accepted["bundle_id"], ticket_for(accepted, cid=str(uuid4())))
    assert status == 403 and events[0][1]["detail"]["code"] == "ticket_correlation_mismatch"


def test_ticket_issued_to_another_user_is_refused_when_bundle_names_its_user(client: TestClient, fixture_payload: dict) -> None:
    """Fail closed: when the module binds the bundle to a user, a ticket for someone else does not open it."""
    accepted = post_bundle(client, fixture_payload, user_uuid=USER_UUID)
    assert read_events(client, accepted["bundle_id"], ticket_for(accepted, sub=USER_UUID))[0] == 200
    status, _, events = read_events(client, accepted["bundle_id"], ticket_for(accepted, sub=str(uuid4())))
    assert status == 403 and events[0][1]["detail"]["code"] == "ticket_user_mismatch"


@pytest.mark.parametrize(
    ("token_fn", "expected_status", "expected_code"),
    [
        (lambda a: ticket_for(a, iat=int(time.time()) - 300, exp=int(time.time()) - 60), 401, "expired_ticket"),
        (lambda a: ticket_for(a, secret="another-secret-that-is-long-enough-123"), 401, "invalid_ticket"),
        (lambda a: "garbage", 401, "invalid_ticket"),
        (lambda a: "", 401, "invalid_ticket"),
    ],
    ids=["expired", "wrong-secret", "garbage", "empty"],
)
def test_invalid_tickets_are_401(client: TestClient, fixture_payload: dict, token_fn, expected_status: int, expected_code: str) -> None:
    """Guards: signature and expiry are checked before any bundle lookup; the error body has no clinical content."""
    accepted = post_bundle(client, fixture_payload)
    status, _, events = read_events(client, accepted["bundle_id"], token_fn(accepted))
    assert status == expected_status and events[0][1]["detail"]["code"] == expected_code


def test_missing_authorization_header_is_401(client: TestClient, fixture_payload: dict) -> None:
    accepted = post_bundle(client, fixture_payload)
    resp = client.get(f"/v1/briefings/{accepted['bundle_id']}")
    assert resp.status_code == 401 and resp.json()["detail"]["code"] == "invalid_ticket"


def test_unknown_bundle_with_matching_ticket_is_404(client: TestClient, fixture_payload: dict) -> None:
    """Boundary: a ticket for a bundle that never existed (or expired) is a 404, never a fabricated briefing."""
    accepted = post_bundle(client, fixture_payload)
    ghost = {**accepted, "bundle_id": str(uuid4())}
    status, _, events = read_events(client, ghost["bundle_id"], ticket_for(ghost))
    assert status == 404 and events[0][1]["detail"]["code"] == "bundle_not_found"


def test_briefing_stream_without_secret_configured_is_503(unconfigured_client: TestClient) -> None:
    resp = unconfigured_client.get(f"/v1/briefings/{uuid4()}", headers=bearer("x.y.z"))
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "not_configured"


# --------------------------------------------------------------------------- #
# Degradation inside the stream
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider_script", [[ProviderUnavailableError("down")]])
def test_provider_failure_streams_an_explicit_degraded_event(client: TestClient, fixture_payload: dict) -> None:
    """Invariant: a model outage is a `degraded` event with a reason code, never an empty `complete`."""
    accepted = post_bundle(client, fixture_payload)
    status, _, events = read_events(client, accepted["bundle_id"], ticket_for(accepted))
    assert status == 200
    assert [n for n, _ in events] == ["degraded"]
    degraded = events[0][1]
    assert degraded["stage"] == "extraction" and degraded["reason_code"] == "provider_unavailable"
    assert degraded["deterministic_sections_intact"] is True
    assert degraded["correlation_id"] == accepted["correlation_id"] and degraded["patient_uuid"] == accepted["patient_uuid"]


class SlowProvider(FakeProvider):
    async def extract_commitments(self, plan_text: str):  # type: ignore[override]
        await asyncio.sleep(5)
        return await super().extract_commitments(plan_text)


@pytest.mark.parametrize("service_settings", [configured_settings(briefing_timeout_seconds=0.2)])
def test_hard_timeout_streams_degraded_timeout(client: TestClient, fixture_payload: dict, fake_provider: FakeProvider) -> None:
    """Failure mode: the hard timeout produces `degraded{timeout}` instead of an open connection."""
    slow = SlowProvider(model_output(metformin(), hba1c()))
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: slow)
    accepted = post_bundle(client, fixture_payload)
    status, _, events = read_events(client, accepted["bundle_id"], ticket_for(accepted))
    assert status == 200 and events == [("degraded", events[0][1])]
    assert events[0][1]["reason_code"] == "timeout" and events[0][1]["stage"] == "extraction"


# --------------------------------------------------------------------------- #
# DELETE /v1/bundles/{bundle_id}
# --------------------------------------------------------------------------- #


def test_delete_drops_the_bundle_so_later_tickets_fail(client: TestClient, fixture_payload: dict) -> None:
    """Patient switch: after delete, even a fresh valid ticket cannot read the bundle (stale-event guard)."""
    accepted = post_bundle(client, fixture_payload)
    resp = client.delete(f"/v1/bundles/{accepted['bundle_id']}", headers=bearer(ticket_for(accepted)))
    assert resp.status_code == 204 and resp.headers[CORRELATION_HEADER] == accepted["correlation_id"]
    status, _, events = read_events(client, accepted["bundle_id"], ticket_for(accepted))
    assert status == 404 and events[0][1]["detail"]["code"] == "bundle_not_found"
    # Idempotent.
    assert client.delete(f"/v1/bundles/{accepted['bundle_id']}", headers=bearer(ticket_for(accepted))).status_code == 204


def test_delete_accepts_an_expired_or_used_ticket_but_not_another_bundles(client: TestClient, fixture_payload: dict) -> None:
    """Deletion only removes data, so an expired/used ticket may still delete its own bundle - never a different one."""
    a = post_bundle(client, fixture_payload)
    b = post_bundle(client, fixture_payload, correlation_id=str(uuid4()), patient_uuid=str(uuid4()))
    used = ticket_for(a)
    assert read_events(client, a["bundle_id"], used)[0] == 200

    resp = client.delete(f"/v1/bundles/{b['bundle_id']}", headers=bearer(used))
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "ticket_bundle_mismatch"
    assert read_events(client, b["bundle_id"], ticket_for(b))[0] == 200  # B untouched

    expired = ticket_for(a, iat=int(time.time()) - 300, exp=int(time.time()) - 60)
    assert client.delete(f"/v1/bundles/{a['bundle_id']}", headers=bearer(expired)).status_code == 204
    assert client.delete(f"/v1/bundles/{a['bundle_id']}", headers=bearer(used)).status_code == 204

    resp = client.delete(f"/v1/bundles/{a['bundle_id']}")
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #


def test_ready_reports_each_dependency_and_503_when_any_is_missing(client: TestClient) -> None:
    """Operability: /ready names what is missing (the API key is blanked by conftest) and returns 503."""
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["dependencies"]["ticket_secret"]["status"] == "ok"
    assert body["dependencies"]["model_provider"]["status"] == "not_configured"
    assert body["dependencies"]["bundle_store"]["status"] == "ok"
    assert TEST_TICKET_SECRET not in resp.text


def test_ready_is_200_when_everything_is_configured(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    resp = client.get("/ready")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"
    assert "test-key-not-real" not in resp.text


def test_ready_without_secret_names_the_gap(unconfigured_client: TestClient) -> None:
    body = unconfigured_client.get("/ready").json()
    assert body["dependencies"]["ticket_secret"]["status"] == "not_configured"


def test_health_needs_nothing(unconfigured_client: TestClient) -> None:
    assert unconfigured_client.get("/health").json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# Contract: the stored bundle is the same contract the sync path accepts
# --------------------------------------------------------------------------- #


def test_bundle_contract_is_shared_with_the_sync_path(fixture_payload: dict) -> None:
    """Regression guard: `POST /v1/bundles` takes the `context` object of a BriefingRequest, unchanged."""
    parsed = BriefingRequest.model_validate(fixture_payload)
    assert parsed.context.user_uuid is None  # optional, schema-additive within 1.0
    assert json.loads(bundle_bytes(fixture_payload)) == fixture_payload["context"]
