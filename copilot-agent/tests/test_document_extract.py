"""POST /v1/documents/extract - extraction only, one document, signed (contract C4, ADR-012).

The module calls this once per unprocessed document on chart open, stores the
extraction JSON, and later sends it back to the briefing route. So the route
must never brief, never retrieve, and must say plainly - with a fixed code -
when it could not extract.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_provider_factory, get_settings
from app.providers.base import ProviderConfigurationError, ProviderUnavailableError
from app.providers.prompt import LAB_EXTRACTION_PROMPT_VERSION
from app.providers.stub_provider import StubProvider
from app.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign_body
from tests.conftest import TEST_TICKET_SECRET, configured_settings

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"
PATIENT = "a2c3ab57-cdd6-4aad-afc9-e19c171e7ed7"
ROUTE = "/v1/documents/extract"


def extract_body(pdf: str = "lab_hba1c_clean.pdf", **over: object) -> bytes:
    payload: dict[str, Any] = {
        "correlation_id": str(uuid.uuid4()),
        "patient_uuid": PATIENT,
        "document_id": 101,
        "doc_type": "lab_pdf",
        "media_type": "application/pdf",
        "document_base64": base64.b64encode((FIXTURES / pdf).read_bytes()).decode(),
    }
    payload.update(over)
    return json.dumps(payload).encode()


def signed(body: bytes) -> dict[str, str]:
    ts = int(time.time())
    return {"Content-Type": "application/json", TIMESTAMP_HEADER: str(ts), SIGNATURE_HEADER: sign_body(TEST_TICKET_SECRET, body, ts)}


def _client(provider_factory: Any) -> Iterator[TestClient]:
    app.dependency_overrides[get_provider_factory] = lambda: provider_factory
    app.dependency_overrides[get_settings] = lambda: configured_settings()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_provider_factory, None)
        app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
def stub_client() -> Iterator[TestClient]:
    yield from _client(StubProvider)


class _NeverCalled(StubProvider):
    """Fails the test if any model call is made."""

    async def parse_structured(self, **kwargs: Any) -> Any:  # pragma: no cover - reaching it is the failure
        raise AssertionError("no model call was expected")


class _ExtractionDown(StubProvider):
    async def parse_structured(self, **kwargs: Any) -> Any:
        raise ProviderUnavailableError("down")


def _post(client: TestClient, body: bytes) -> Any:
    return client.post(ROUTE, content=body, headers=signed(body))


def test_a_signed_lab_pdf_returns_the_extraction_and_nothing_else(stub_client: TestClient) -> None:
    body = extract_body()
    r = _post(stub_client, body)
    assert r.status_code == 200
    d = r.json()
    sent = json.loads(body)
    assert (d["correlation_id"], d["patient_uuid"], d["document_id"], d["doc_type"]) == (
        sent["correlation_id"], PATIENT, 101, "lab_pdf"
    )
    assert d["status"] == "ok" and d["degraded_reason"] is None
    assert d["prompt_version"] == LAB_EXTRACTION_PROMPT_VERSION
    assert d["extraction_model"] == "stub-deterministic"
    extraction = d["extraction"]
    assert extraction["document_id"] == 101 and extraction["results"]
    # Every citation names the document it was read from.
    assert {res["citation"]["source_id"] for res in extraction["results"]} == {"101"}
    assert all("verification_status" in res for res in extraction["results"])
    # Extraction only: no briefing, no routing, no guideline evidence.
    assert not {"briefing", "routing", "provenance", "rendered_text"} & set(d)
    assert "printed_identity" in d


def test_the_extraction_json_round_trips_into_a_lab_document(stub_client: TestClient) -> None:
    """What the module stores is exactly what the briefing route will accept back."""
    from app.documents import LabDocument

    body = extract_body()
    extraction = _post(stub_client, body).json()["extraction"]
    assert LabDocument.model_validate(extraction).document_id == 101


def test_an_unsigned_request_is_refused(stub_client: TestClient) -> None:
    body = extract_body()
    assert stub_client.post(ROUTE, content=body, headers={"Content-Type": "application/json"}).status_code == 401


def test_a_tampered_body_is_refused(stub_client: TestClient) -> None:
    body = extract_body()
    headers = signed(body)
    assert stub_client.post(ROUTE, content=body.replace(b"101", b"102"), headers=headers).status_code == 401


def test_an_intake_form_is_not_supported_yet_and_calls_no_model() -> None:
    for c in _client(_NeverCalled):
        body = extract_body(doc_type="intake_form")
        r = _post(c, body)
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "degraded"
    assert d["degraded_reason"] == "doc_type_not_supported_yet"
    assert d["doc_type"] == "intake_form"
    assert d["extraction"] is None and d["printed_identity"] is None


@pytest.mark.parametrize("doc_type", ["unsupported", "lab", "", None])
def test_an_unknown_doc_type_fails_the_contract(stub_client: TestClient, doc_type: object) -> None:
    body = extract_body(doc_type=doc_type)
    assert _post(stub_client, body).status_code == 422


@pytest.mark.parametrize("field", ["doc_type", "document_base64", "media_type", "document_id"])
def test_a_missing_required_field_fails_the_contract(stub_client: TestClient, field: str) -> None:
    payload = json.loads(extract_body())
    del payload[field]
    body = json.dumps(payload).encode()
    assert _post(stub_client, body).status_code == 422


def test_an_unknown_field_fails_the_contract(stub_client: TestClient) -> None:
    body = extract_body(patient_name="nobody")
    assert _post(stub_client, body).status_code == 422


def test_a_gif_is_not_an_accepted_media_type(stub_client: TestClient) -> None:
    body = extract_body(media_type="image/gif")
    assert _post(stub_client, body).status_code == 422


def test_an_undecodable_document_degrades_with_a_fixed_code(stub_client: TestClient) -> None:
    body = extract_body(document_base64="!!!not base64!!!")
    r = _post(stub_client, body)
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["degraded_reason"], r.json()["extraction"]) == ("degraded", "document_not_decodable", None)


def test_a_model_outage_degrades_rather_than_returning_an_empty_extraction() -> None:
    for c in _client(_ExtractionDown):
        body = extract_body()
        r = _post(c, body)
    assert r.status_code == 200
    d = r.json()
    assert (d["status"], d["degraded_reason"], d["extraction"]) == ("degraded", "extraction_unavailable", None)
    assert d["prompt_version"] == LAB_EXTRACTION_PROMPT_VERSION


def test_no_configured_provider_degrades_rather_than_erroring() -> None:
    def _no_provider() -> StubProvider:
        raise ProviderConfigurationError("ANTHROPIC_API_KEY is not configured")

    for c in _client(_no_provider):
        body = extract_body()
        r = _post(c, body)
    assert r.status_code == 200
    assert r.json()["degraded_reason"] == "provider_not_configured"


# --------------------------------------------------------------------------- #
# printed_identity (contract C2): passed through to the module, top level
# --------------------------------------------------------------------------- #


def test_the_printed_identity_is_passed_through_when_the_extractor_supplies_one(
    stub_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lane 1 adds ``LabDocument.printed_identity``; this route reads it if present."""
    import app.document_briefing as db

    real = db.extract_lab_document

    class _Identity:
        name = "Synthetic Person"
        dob = date(1960, 2, 29)

    async def _with_identity(**kwargs: Any) -> Any:
        document = await real(**kwargs)
        object.__setattr__(document, "printed_identity", _Identity())
        return document

    monkeypatch.setattr(db, "extract_lab_document", _with_identity)
    body = extract_body()
    d = _post(stub_client, body).json()
    assert d["printed_identity"] == {"name": "Synthetic Person", "dob": "1960-02-29"}


def test_no_printed_identity_is_null_not_an_empty_object(stub_client: TestClient) -> None:
    body = extract_body()
    d = _post(stub_client, body).json()
    assert d["printed_identity"] is None or set(d["printed_identity"]) == {"name", "dob"}
