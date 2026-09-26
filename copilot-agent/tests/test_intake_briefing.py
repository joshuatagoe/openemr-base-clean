"""Stored intake forms in the document briefing (ADR-010, contract C4).

Intake items are what the patient reported: every one is a patient-reported
line citing the form, never a chart fact and never a document-stated lab value.
A blank allergy section is named as a limitation, never read as "no known
allergies". Reported medications are compared with the chart's current
medication list when the module sends it (``chart_medications``); without it,
nothing is compared - and the briefing says so.
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


# --------------------------------------------------------------------------- #
# Chart medications (ADR-010 medication conflicts)
# --------------------------------------------------------------------------- #


def _chart_med(rid: str, drug: str, dosage: str | None = None, *, active: bool | None = True) -> dict[str, Any]:
    """One chart medication in the Week 1 bundle's shape (ContextBundleBuilder::mapMedication)."""
    source = rid.split(":", 1)[0]
    flag = "active" if source == "prescriptions" else "activity"
    return {
        "record_id": rid,
        "source_table": source,
        "drug_name": drug,
        "rxnorm_code": None,
        "dosage_text": dosage,
        "active": active,
        "status_field": f"{flag},end_date",
        "status_value": f"{flag}=1,end_date=null",
        "started_at": "2026-01-10T09:00:00+00:00",
        "ended_at": None,
        "modified_at": None,
        "timestamp": "2026-01-10T09:00:00+00:00",
        "timestamp_field": "date_added",
    }


async def _brief_with_meds(chart_medications: list[dict[str, Any]] | None):
    over = {} if chart_medications is None else {"chart_medications": chart_medications}
    request = DocumentBriefingRequest.model_validate(_payload([_stored_intake(301, await _intake(301))], **over))
    return await run_supervised_briefing(request, provider=_AnswerOnly(), reranker=build_reranker("fake", region="us-east-1"))


def _conflicts(response) -> list:
    return [line for line in response.briefing.needs_attention if line.tier is AssertionTier.PATIENT_REPORTED and "Patient reports" in line.text]


async def test_a_reported_medication_missing_from_the_chart_is_a_conflict_citing_the_form_item() -> None:
    # Evelyn's form reports Metformin 500 mg twice daily, Lisinopril 10 mg daily, Atorvastatin 20 mg nightly.
    chart = [
        _chart_med("prescriptions:11", "Metformin 500 mg tablet", "1 tab by mouth twice daily"),
        _chart_med("lists:12", "Lisinopril 10 mg", "daily"),
        _chart_med("prescriptions:13", "Amlodipine 5 mg", "daily"),  # on the chart, not on the form: never flagged
    ]
    response = await _brief_with_meds(chart)
    conflicts = _conflicts(response)
    assert len(conflicts) == 1, [c.text for c in conflicts]
    line = conflicts[0]
    assert line.text.startswith("Patient reports Atorvastatin 20 mg nightly on the intake form; not on the chart medication list")
    assert "3 current" in line.text and "checked" in line.text  # says the list was checked
    assert line.document_citation.source_id == "301" and "Atorvastatin" in line.document_citation.quote_or_value
    assert line.record_citation is None and line.not_yet_in_chart
    assert not any("Amlodipine" in c.text for c in conflicts)
    assert not any("not compared with the chart" in lim for lim in response.briefing.limitations)
    assert any("compared with the chart's current medication list" in lim for lim in response.briefing.limitations)


async def test_the_same_drug_at_a_different_dose_or_frequency_cites_the_form_item_and_the_chart_record() -> None:
    chart = [
        _chart_med("prescriptions:11", "Metformin", "1000 mg twice daily"),  # dose differs
        _chart_med("lists:12", "Lisinopril 10 mg", "twice daily"),  # frequency differs
        _chart_med("prescriptions:13", "Atorvastatin 20 mg", "at bedtime"),  # same: nightly == at bedtime
    ]
    conflicts = {c.record_citation.record_id: c for c in _conflicts(await _brief_with_meds(chart)) if c.record_citation}
    assert set(conflicts) == {"prescriptions:11", "lists:12"}
    metformin = conflicts["prescriptions:11"]
    assert "Metformin 500 mg twice daily" in metformin.text and "1000 mg twice daily" in metformin.text
    assert metformin.document_citation.source_id == "301" and "Metformin" in metformin.document_citation.quote_or_value
    assert metformin.record_citation.record_type.value == "medication"
    assert "frequency" in conflicts["lists:12"].text or "dose" in conflicts["lists:12"].text


async def test_an_inactive_chart_record_does_not_count_as_on_the_list_but_an_indeterminate_one_does() -> None:
    chart = [
        _chart_med("prescriptions:11", "Metformin 500 mg", "twice daily"),
        _chart_med("lists:12", "Lisinopril 10 mg", "daily"),
        _chart_med("prescriptions:13", "Atorvastatin 20 mg", "nightly", active=False),
    ]
    conflicts = _conflicts(await _brief_with_meds(chart))
    assert [c.text.split(" on the intake form")[0] for c in conflicts] == ["Patient reports Atorvastatin 20 mg nightly"]
    chart[2] = _chart_med("prescriptions:13", "Atorvastatin 20 mg", "nightly", active=None)  # status fields disagree
    assert _conflicts(await _brief_with_meds(chart)) == []


async def test_an_empty_chart_list_flags_every_reported_medication_and_says_it_was_checked() -> None:
    conflicts = _conflicts(await _brief_with_meds([]))
    assert len(conflicts) == 3
    assert all("not on the chart medication list" in c.text and "checked" in c.text for c in conflicts)


async def test_without_chart_medications_nothing_is_compared_and_the_briefing_says_so() -> None:
    response = await _brief_with_meds(None)
    assert _conflicts(response) == []
    assert any("not compared with the chart" in lim for lim in response.briefing.limitations)


async def test_chart_medications_are_bounded_and_strict() -> None:
    from app.document_briefing import MAX_CHART_MEDICATIONS

    documents = [_stored_intake(301, await _intake(301))]
    too_many = [_chart_med(f"prescriptions:{i}", "Metformin") for i in range(1, MAX_CHART_MEDICATIONS + 2)]
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(_payload(documents, chart_medications=too_many))
    extra = _chart_med("prescriptions:1", "Metformin") | {"pid": 7}
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(_payload(documents, chart_medications=[extra]))
    with pytest.raises(ValidationError):
        DocumentBriefingRequest.model_validate(_payload(documents, chart_medications=[_chart_med("prescriptions:1", "")]))
    ok = DocumentBriefingRequest.model_validate(_payload(documents, chart_medications=[_chart_med("prescriptions:1", "Metformin")]))
    assert ok.chart_medications[0].record_id == "prescriptions:1"


async def test_medication_names_never_reach_a_log() -> None:
    import io
    import logging

    from app.observability import JsonFormatter, get_logger

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())  # every structured field, not just the event name
    logger = get_logger()
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        chart = [_chart_med("prescriptions:11", "Metformin 1000 mg", "twice daily"), _chart_med("lists:12", "Zolpidemix 5 mg")]
        response = await _brief_with_meds(chart)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    assert _conflicts(response)
    logged = buf.getvalue()
    assert "briefing.built" in logged
    for needle in ("Metformin", "Lisinopril", "Atorvastatin", "Zolpidemix", "twice daily", "prescriptions:11"):
        assert needle not in logged, needle


#: The keys ContextBundleBuilder::mapMedication emits; MedicationBriefingTest.php pins the same list.
MODULE_MEDICATION_KEYS = [
    "record_id", "source_table", "drug_name", "rxnorm_code", "dosage_text", "active", "status_field",
    "status_value", "started_at", "ended_at", "modified_at", "timestamp", "timestamp_field",
]


def test_chart_medications_have_the_week1_bundle_shape_the_module_sends() -> None:
    from app.contracts import MedicationRecord

    assert list(MedicationRecord.model_fields) == MODULE_MEDICATION_KEYS
    assert list(_chart_med("prescriptions:1", "Metformin")) == MODULE_MEDICATION_KEYS


def test_the_signed_route_accepts_chart_medications_and_returns_conflict_lines() -> None:
    import asyncio
    import json as _json

    from tests.test_document_briefing import _signed
    from tests.test_document_extract import _client

    payload = _payload([_stored_intake(301, asyncio.run(_intake(301)))], chart_medications=[_chart_med("lists:12", "Lisinopril 10 mg", "daily")])
    body = _json.dumps(payload).encode()
    from contextlib import contextmanager

    with contextmanager(_client)(StubProvider) as client:
        r = client.post("/v1/documents/briefing", content=body, headers=_signed(body))
    assert r.status_code == 200, r.text
    attention = r.json()["briefing"]["needs_attention"]
    texts = [line["text"] for line in attention if line["tier"] == "patient_reported"]
    assert sum("not on the chart medication list" in t for t in texts) == 2  # Metformin, Atorvastatin
    assert not any("Lisinopril" in t and "Patient reports" in t for t in texts)
