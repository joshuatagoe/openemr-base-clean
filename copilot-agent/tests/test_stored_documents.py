"""Briefing from stored extractions (contract C4, ADR-012).

The module sends every extracted document's stored JSON plus the chart's lab
history. The supervisor must then skip extraction entirely, the briefing must
cover every listed document, and every citation must name the document it came
from. The legacy single-document path (``document_base64``) keeps working for
one release; its own tests live in test_document_briefing.py/test_workflow.py.
"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.document_briefing import (
    BriefingStatus,
    ConsiderationDraftSet,
    DocumentBriefingRequest,
    build_reranker,
)
from app.lab_extractor import extract_lab_document
from app.providers.stub_provider import StubProvider
from app.workflow import run_supervised_briefing
from tests.test_document_extract import _client, signed

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"
PATIENT = "a2c3ab57-cdd6-4aad-afc9-e19c171e7ed7"


class _AnswerOnly(StubProvider):
    """The answer model works; any other model call (an extraction) fails the test."""

    async def parse_structured(self, *, schema: Any, **kwargs: Any) -> Any:
        if schema is not ConsiderationDraftSet:
            raise AssertionError("a stored extraction must not be re-extracted")
        return await super().parse_structured(schema=schema, **kwargs)


async def _extraction(document_id: int, pdf: str = "lab_hba1c_clean.pdf") -> dict[str, Any]:
    document = await extract_lab_document(
        document_id=document_id, pdf_bytes=(FIXTURES / pdf).read_bytes(), media_type="application/pdf", provider=StubProvider()
    )
    return document.model_dump(mode="json")


def _prior(test_name: str = "Hemoglobin A1c", value: str = "7.1", when: str = "2026-03-01T09:00:00+00:00", rid: str = "procedure_result:9001") -> dict[str, Any]:
    return {
        "result_id": rid,
        "order_id": "procedure_order:12",
        "test_name": test_name,
        "code": "4548-4",
        "value": value,
        "units": "%",
        "abnormal_flag": None,
        "range": "4.0-5.6",
        "status": "final",
        "observed_at": when,
    }


def _payload(documents: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "correlation_id": str(uuid.uuid4()),
        "patient_uuid": PATIENT,
        "documents": documents,
        "prior_facts": [],
    }
    payload.update(over)
    return payload


def _stored(document_id: int, extraction: dict[str, Any]) -> dict[str, Any]:
    return {"document_id": document_id, "doc_type": "lab_pdf", "extraction": extraction}


async def _run(payload: dict[str, Any], provider: Any = None) -> Any:
    request = DocumentBriefingRequest.model_validate(payload)
    return await run_supervised_briefing(request, provider=provider or _AnswerOnly(), reranker=build_reranker("fake", region="us-east-1"))


# --------------------------------------------------------------------------- #
# The supervisor skips extraction
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_stored_documents_route_straight_to_evidence_retrieval() -> None:
    response = await _run(_payload([_stored(201, await _extraction(201))]))
    assert response.status is BriefingStatus.OK
    assert [(d.step, d.target, d.reason_code) for d in response.routing] == [
        (1, "evidence-retriever", "evidence_required"),
        (2, "answer", "evidence_ready"),
        (3, "finish", "briefing_complete"),
    ]
    assert response.briefing is not None and response.briefing.what_to_consider


@pytest.mark.anyio
async def test_a_briefing_covers_every_listed_document_and_each_citation_names_its_document() -> None:
    documents = [_stored(201, await _extraction(201)), _stored(202, await _extraction(202, "lab_hba1c_degraded_scan.pdf"))]
    response = await _run(_payload(documents))
    assert response.document_ids == (201, 202)
    assert response.document_id == 201  # the first listed, kept for callers that read one id
    briefing = response.briefing
    assert briefing is not None
    lines = briefing.what_changed + briefing.needs_attention
    cited = {line.document_citation.source_id for line in lines if line.document_citation is not None}
    assert cited == {"201", "202"}, "both documents must reach the briefing, each under its own id"
    for consideration in briefing.what_to_consider:
        assert all(f.document_citation is None or f.document_citation.source_id in {"201", "202"} for f in consideration.facts)
    # Line ids stay unique across documents.
    assert len({line.line_id for line in lines}) == len(lines)
    # The obscured value on the second document is still never guessed.
    assert any(line.document_citation.source_id == "202" and "unreadable" in line.text for line in briefing.needs_attention)


@pytest.mark.anyio
async def test_a_document_level_collection_date_is_kept_per_result_when_documents_are_combined() -> None:
    first = await _extraction(201)
    first["collection_date"] = "2026-09-01"
    for r in first["results"]:
        r["collection_date"] = None
    response = await _run(_payload([_stored(201, first), _stored(202, await _extraction(202))]))
    assert response.briefing is not None
    dated = [line.text for line in response.briefing.what_changed if line.document_citation and line.document_citation.source_id == "201"]
    assert dated and all("2026-09-01" in text for text in dated)


# --------------------------------------------------------------------------- #
# prior_facts: the chart's lab history, as the Week 1 bundle carries it
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_prior_facts_turn_a_new_value_into_a_change_with_the_chart_citation() -> None:
    response = await _run(_payload([_stored(201, await _extraction(201))], prior_facts=[_prior()]))
    assert response.briefing is not None
    a1c = [line for line in response.briefing.what_changed if "A1c" in line.text]
    assert a1c and "7.1" in a1c[0].text
    assert a1c[0].record_citation is not None and a1c[0].record_citation.record_id == "procedure_result:9001"


@pytest.mark.anyio
async def test_the_newest_prior_value_is_the_one_compared_against() -> None:
    prior = [
        _prior(value="7.9", when="2026-06-01T09:00:00+00:00", rid="procedure_result:9002"),
        _prior(value="7.1", when="2026-03-01T09:00:00+00:00", rid="procedure_result:9001"),
    ]
    response = await _run(_payload([_stored(201, await _extraction(201))], prior_facts=prior))
    assert response.briefing is not None
    a1c = next(line for line in response.briefing.what_changed if "A1c" in line.text)
    assert a1c.record_citation is not None and a1c.record_citation.record_id == "procedure_result:9002"


@pytest.mark.anyio
async def test_prior_facts_are_accepted_on_the_legacy_single_document_path_too() -> None:
    payload = {
        "correlation_id": str(uuid.uuid4()),
        "patient_uuid": PATIENT,
        "document_id": 101,
        "media_type": "application/pdf",
        "document_base64": base64.b64encode((FIXTURES / "lab_hba1c_clean.pdf").read_bytes()).decode(),
        "prior_facts": [_prior()],
    }
    response = await _run(payload, provider=StubProvider())
    assert response.routing[0].target == "intake-extractor"
    assert response.document_ids == (101,)
    assert response.briefing is not None
    assert any(line.record_citation is not None for line in response.briefing.what_changed)


# --------------------------------------------------------------------------- #
# The contract fails closed
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_documents_and_a_legacy_document_together_are_ambiguous_and_refused() -> None:
    payload = _payload(
        [_stored(201, await _extraction(201))],
        document_id=101,
        media_type="application/pdf",
        document_base64=base64.b64encode(b"%PDF-1.4").decode(),
    )
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(payload)


def test_neither_documents_nor_a_legacy_document_is_refused() -> None:
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate({"correlation_id": str(uuid.uuid4()), "patient_uuid": PATIENT})


def test_an_empty_documents_list_is_refused() -> None:
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(_payload([]))


@pytest.mark.anyio
async def test_a_legacy_request_missing_its_media_type_is_refused() -> None:
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(
            {"correlation_id": str(uuid.uuid4()), "patient_uuid": PATIENT, "document_id": 101, "document_base64": "JVBERi0="}
        )


@pytest.mark.anyio
async def test_an_extraction_stored_under_another_document_id_is_refused() -> None:
    """A stored extraction attributed to the wrong file would cite the wrong document."""
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(_payload([_stored(202, await _extraction(201))]))


@pytest.mark.anyio
async def test_a_citation_naming_another_document_is_refused() -> None:
    extraction = await _extraction(201)
    extraction["results"][0]["citation"]["source_id"] = "999"
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(_payload([_stored(201, extraction)]))


@pytest.mark.anyio
async def test_the_same_document_listed_twice_is_refused() -> None:
    extraction = await _extraction(201)
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(_payload([_stored(201, extraction), _stored(201, extraction)]))


@pytest.mark.anyio
async def test_an_intake_form_is_not_a_stored_briefing_document_yet() -> None:
    stored = _stored(201, await _extraction(201))
    stored["doc_type"] = "intake_form"
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(_payload([stored]))


# --------------------------------------------------------------------------- #
# Over HTTP: the extract route's output is the briefing route's input
# --------------------------------------------------------------------------- #


def test_extract_then_brief_over_http_never_re_extracts() -> None:
    from tests.test_document_extract import extract_body

    for c in _client(StubProvider):
        stored = []
        for document_id, pdf in ((201, "lab_hba1c_clean.pdf"), (202, "lab_hba1c_degraded_scan.pdf")):
            body = extract_body(pdf, document_id=document_id)
            extracted = c.post("/v1/documents/extract", content=body, headers=signed(body)).json()
            assert extracted["status"] == "ok"
            stored.append(_stored(document_id, extracted["extraction"]))
    for c in _client(_AnswerOnly):
        body = json.dumps(_payload(stored, prior_facts=[_prior()])).encode()
        r = c.post("/v1/documents/briefing", content=body, headers=signed(body))
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok" and d["document_ids"] == [201, 202]
    assert [step["target"] for step in d["routing"]] == ["evidence-retriever", "answer", "finish"]
    assert "document 201" in d["rendered_text"] and "document 202" in d["rendered_text"]


# --------------------------------------------------------------------------- #
# Round trip: what the module stores is what the briefing reads (found by the
# w2_brief_computed_vs_printed_flag golden case, 2026-09-26)
# --------------------------------------------------------------------------- #


def test_a_stored_numeric_value_is_still_a_number_after_the_json_round_trip() -> None:
    from decimal import Decimal

    from app.documents import LabDocument

    import asyncio

    stored = asyncio.run(_extraction(101))  # model_dump(mode="json"): Decimal 8.9 is the JSON string "8.9"
    assert stored["results"][0]["value"] == "8.9"
    document = LabDocument.model_validate(stored)
    assert document.results[0].value == Decimal("8.9") and isinstance(document.results[0].value, Decimal)
    text = next(r for r in LabDocument.model_validate({**stored, "results": [
        {**stored["results"][0], "value": "Negative"}]}).results)
    assert text.value == "Negative"  # a non-numeric result stays text


@pytest.mark.anyio
async def test_a_stored_value_above_its_printed_range_is_still_a_computed_line() -> None:
    from app.briefing import AssertionTier

    request = DocumentBriefingRequest.model_validate(_payload([_stored(101, await _extraction(101))]))
    response = await run_supervised_briefing(request, provider=_AnswerOnly(), reranker=build_reranker("fake", region="us-east-1"))
    computed = [line for line in response.briefing.needs_attention if line.tier is AssertionTier.COMPUTED]
    assert {line.computed.test_name for line in computed} >= {"Hemoglobin A1c"}
