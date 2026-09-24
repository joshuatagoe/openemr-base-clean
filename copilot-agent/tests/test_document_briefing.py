"""POST /v1/documents/briefing - the Week 2 flow, end to end through the real app.

Runs offline: StubProvider for both model calls, FakeReranker for ranking. The
live path uses the same code with a different provider, and its responses are
exercised separately by the replay harness.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_provider_factory, get_settings
from app.providers.base import ProviderConfigurationError
from app.providers.stub_provider import StubProvider
from app.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign_body
from tests.conftest import TEST_TICKET_SECRET, configured_settings

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"
PATIENT = "a2c3ab57-cdd6-4aad-afc9-e19c171e7ed7"


def _body(pdf: str = "lab_hba1c_clean.pdf", **over: object) -> bytes:
    payload = {
        "correlation_id": str(uuid.uuid4()),
        "patient_uuid": PATIENT,
        "document_id": 101,
        "media_type": "application/pdf",
        "document_base64": base64.b64encode((FIXTURES / pdf).read_bytes()).decode(),
        "question": None,
    }
    payload.update(over)
    return json.dumps(payload).encode()


def _signed(body: bytes) -> dict[str, str]:
    ts = int(time.time())
    return {"Content-Type": "application/json", TIMESTAMP_HEADER: str(ts), SIGNATURE_HEADER: sign_body(TEST_TICKET_SECRET, body, ts)}


@pytest.fixture
def stub_client() -> Iterator[TestClient]:
    app.dependency_overrides[get_provider_factory] = lambda: StubProvider
    app.dependency_overrides[get_settings] = lambda: configured_settings()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_provider_factory, None)
        app.dependency_overrides.pop(get_settings, None)


def test_a_signed_request_returns_all_three_headings(stub_client: TestClient) -> None:
    body = _body()
    r = stub_client.post("/v1/documents/briefing", content=body, headers=_signed(body))
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok"
    b = d["briefing"]
    assert b["what_changed"] and b["needs_attention"] and b["what_to_consider"]


def test_an_unsigned_request_is_refused(stub_client: TestClient) -> None:
    """The document only ever reaches the agent through a verified signature."""
    body = _body()
    r = stub_client.post("/v1/documents/briefing", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 401


def test_a_tampered_body_is_refused(stub_client: TestClient) -> None:
    body = _body()
    headers = _signed(body)
    r = stub_client.post("/v1/documents/briefing", content=body.replace(b"101", b"102"), headers=headers)
    assert r.status_code == 401


def test_an_out_of_range_value_with_no_printed_flag_is_computed_not_printed(stub_client: TestClient) -> None:
    """The most consequential display error: a computed comparison styled as a lab flag."""
    body = _body()
    b = stub_client.post("/v1/documents/briefing", content=body, headers=_signed(body)).json()["briefing"]
    a1c = [line for line in b["needs_attention"] if "A1c" in line["text"]]
    assert a1c, "8.9 against 4.0-5.6 must reach Needs attention"
    assert all(line["tier"] == "computed" for line in a1c)
    assert all(line["abnormal_flag_source"] != "extracted" for line in a1c)


def test_extracted_values_are_marked_not_yet_in_the_chart(stub_client: TestClient) -> None:
    """ADR-003: nothing is filed until a clinician verifies it."""
    body = _body()
    b = stub_client.post("/v1/documents/briefing", content=body, headers=_signed(body)).json()["briefing"]
    assert all(line["not_yet_in_chart"] for line in b["what_changed"])


def test_every_guideline_citation_carries_population_scope_and_tier(stub_client: TestClient) -> None:
    body = _body()
    b = stub_client.post("/v1/documents/briefing", content=body, headers=_signed(body)).json()["briefing"]
    cites = [c for con in b["what_to_consider"] for c in con["citations"]]
    assert cites
    assert all(c["population_scope"] and c["evidence_tier"] in ("A", "B") for c in cites)


def test_provenance_names_the_reranker_that_actually_ran(stub_client: TestClient) -> None:
    """A lexical fallback must never read as Cohere."""
    body = _body()
    p = stub_client.post("/v1/documents/briefing", content=body, headers=_signed(body)).json()["provenance"]
    assert "fake-lexical" in p["reranker"]
    assert p["corpus_version"]


def test_an_obscured_scan_value_is_never_guessed(stub_client: TestClient) -> None:
    body = _body("lab_hba1c_degraded_scan.pdf")
    b = stub_client.post("/v1/documents/briefing", content=body, headers=_signed(body)).json()["briefing"]
    rendered = json.dumps(b)
    assert '"8.9"' not in rendered.replace('"8.#"', "")


def test_a_dosing_question_earns_the_fixed_refusal(stub_client: TestClient) -> None:
    body = _body(question="Should I increase her metformin dose?")
    b = stub_client.post("/v1/documents/briefing", content=body, headers=_signed(body)).json()["briefing"]
    assert b["refusal"]


def test_an_undecodable_document_degrades_rather_than_erroring(stub_client: TestClient) -> None:
    body = _body(document_base64="!!!not base64!!!")
    r = stub_client.post("/v1/documents/briefing", content=body, headers=_signed(body))
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["degraded_reason"] == "document_not_decodable"


def test_no_configured_provider_degrades_rather_than_erroring() -> None:
    def _no_provider() -> StubProvider:
        raise ProviderConfigurationError("ANTHROPIC_API_KEY is not configured")

    app.dependency_overrides[get_provider_factory] = lambda: _no_provider
    app.dependency_overrides[get_settings] = lambda: configured_settings()
    try:
        with TestClient(app) as c:
            body = _body()
            r = c.post("/v1/documents/briefing", content=body, headers=_signed(body))
    finally:
        app.dependency_overrides.pop(get_provider_factory, None)
        app.dependency_overrides.pop(get_settings, None)
    assert r.status_code == 200
    assert r.json()["degraded_reason"] == "provider_not_configured"
