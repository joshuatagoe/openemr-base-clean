"""Stored intake forms in the document briefing (ADR-010, contract C4).

Intake items are what the patient reported: every one is a patient-reported
line citing the form, never a chart fact and never a document-stated lab value.
A blank allergy section is named as a limitation, never read as "no known
allergies". Chart medications are not in the briefing contract, so reported
medications are not compared with them - and the briefing says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.briefing import AssertionTier
from app.document_briefing import BriefingStatus, DocumentBriefingRequest, build_reranker, stored_form
from app.intake_extractor import IntakeDraft, MedicationDraft, extract_intake_document
from app.page_text import FakeOcr
from app.providers.stub_provider import StubProvider
from app.workflow import run_supervised_briefing
from tests.fakes import FakeProvider
from tests.test_stored_documents import _AnswerOnly, _extraction, _payload, _stored

pytestmark = pytest.mark.anyio

INTAKE = Path(__file__).resolve().parent.parent / "fixtures" / "documents" / "intake"


async def _intake(document_id: int, pdf: str = "intake_typed_evelyn.pdf", provider: Any = None) -> dict[str, Any]:
    form = await extract_intake_document(
        document_id=document_id, document_bytes=(INTAKE / pdf).read_bytes(), media_type="application/pdf",
        provider=provider or StubProvider(), ocr=FakeOcr(),
    )
    return stored_form(form).model_dump(mode="json")  # what the module stores


def _stored_intake(document_id: int, extraction: dict[str, Any]) -> dict[str, Any]:
    return {"document_id": document_id, "doc_type": "intake_form", "extraction": extraction}


async def _brief(documents: list[dict[str, Any]]):
    request = DocumentBriefingRequest.model_validate(_payload(documents))
    return await run_supervised_briefing(request, provider=_AnswerOnly(), reranker=build_reranker("fake", region="us-east-1"))


async def test_intake_items_are_patient_reported_lines_citing_the_form() -> None:
    response = await _brief([_stored(101, await _extraction(101)), _stored_intake(301, await _intake(301))])
    assert response.status is BriefingStatus.OK
    assert response.document_ids == (101, 301)
    reported = [line for line in response.briefing.what_changed if line.tier is AssertionTier.PATIENT_REPORTED]
    texts = [line.text for line in reported]
    assert any("Metformin 500 mg twice daily" in t and "current medication" in t for t in texts)
    assert any("Penicillin" in t for t in texts) and any("Tired and thirsty" in t for t in texts)
    assert all(line.document_citation.source_id == "301" and line.not_yet_in_chart for line in reported)
    assert all(line.document_citation.bbox is not None for line in reported)  # verified from the text layer
    # Lab lines are still there, and stay document-stated.
    assert any(line.tier is AssertionTier.DOCUMENT_STATED for line in response.briefing.what_changed)
    assert "[patient-reported]" in response.rendered_text


async def test_an_intake_only_briefing_works_and_proposes_nothing_from_labs() -> None:
    response = await _brief([_stored_intake(301, await _intake(301))])
    assert response.status is BriefingStatus.OK
    assert response.briefing.what_changed
    assert all(line.tier is AssertionTier.PATIENT_REPORTED for line in response.briefing.what_changed)


async def test_a_blank_allergy_section_is_a_limitation_never_no_known_allergies() -> None:
    response = await _brief([_stored_intake(302, await _intake(302, "intake_typed_blank_allergies.pdf"))])
    briefing = response.briefing
    assert any("allerg" in lim and "not a statement of no known allergies" in lim for lim in briefing.limitations)
    lines = briefing.what_changed + briefing.needs_attention
    assert not any("no known allerg" in line.text.lower() for line in lines)


async def test_reported_medications_are_not_compared_with_the_chart_and_the_briefing_says_so() -> None:
    response = await _brief([_stored_intake(301, await _intake(301))])
    assert any("not compared with the chart" in lim for lim in response.briefing.limitations)


async def test_an_unreadable_entry_needs_attention_and_carries_no_box() -> None:
    draft = IntakeDraft(current_medications=[MedicationDraft(name="", dose="500 mg", quote="M#### 500 mg", unreadable=True)])
    response = await _brief([_stored_intake(301, await _intake(301, provider=FakeProvider(draft)))])
    attention = [line for line in response.briefing.needs_attention if line.tier is AssertionTier.PATIENT_REPORTED]
    assert len(attention) == 1 and "could not be read" in attention[0].text
    assert attention[0].document_citation.bbox is None


async def test_a_stored_intake_must_be_labelled_as_one_and_name_its_own_document() -> None:
    extraction = await _intake(301)
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(_payload([{"document_id": 301, "doc_type": "lab_pdf", "extraction": extraction}]))
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(_payload([_stored_intake(302, extraction)]))
    lab = await _extraction(101)
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(_payload([_stored_intake(101, lab)]))
