"""Intake-form extraction (CR1/CR2, ADR-010, ADR-007): read by the model, verified by the matcher."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from app.documents import VerificationStatus
from app.intake import intake_items
from app.intake_extractor import (
    AllergyDraft,
    FamilyHistoryDraft,
    IntakeDraft,
    MedicationDraft,
    extract_intake_document,
)
from app.page_text import FakeOcr, Word
from app.providers.prompt import INTAKE_EXTRACTION_PROMPT_VERSION
from tests.fakes import FakeProvider

pytestmark = pytest.mark.anyio

INTAKE = Path(__file__).resolve().parent.parent / "fixtures" / "documents" / "intake"
EVELYN = INTAKE / "intake_typed_evelyn.pdf"
BLANK = INTAKE / "intake_typed_blank_allergies.pdf"
PHOTO = INTAKE / "intake_handwritten.jpg"


async def _extract(path: Path, provider=None, ocr=None, media_type: str = "application/pdf"):
    return await extract_intake_document(
        document_id=301, document_bytes=path.read_bytes(), media_type=media_type, provider=provider, ocr=ocr or FakeOcr()
    )


async def test_the_stub_reads_a_typed_form_and_the_page_verifies_every_item() -> None:
    form = await _extract(EVELYN)
    assert form.document_id == 301 and form.doc_type == "intake_form"
    assert form.chief_concern is not None and form.chief_concern.value == "Tired and thirsty for three weeks"
    assert [(m.name, m.dose, m.frequency) for m in form.current_medications] == [
        ("Metformin", "500 mg", "twice daily"),
        ("Lisinopril", "10 mg", "daily"),
        ("Atorvastatin", "20 mg", "nightly"),
    ]
    assert [(a.substance, a.reaction) for a in form.allergies] == [("Penicillin", "hives")]
    assert [(f.relation, f.condition) for f in form.family_history] == [
        ("Mother", "type 2 diabetes"),
        ("Father", "heart attack at 58"),
    ]
    items = intake_items(form)
    assert items and all(i.verification_status is VerificationStatus.VERIFIED_EXACT for i in items)
    assert all(i.citation.page == 1 and i.citation.bbox is not None for i in items)
    assert {c.source_id for c in (i.citation for i in items)} == {"301"}
    assert form.extraction_metadata.prompt_version == INTAKE_EXTRACTION_PROMPT_VERSION
    assert form.extraction_metadata.verified_fraction == 1.0


async def test_demographics_are_read_and_the_identity_is_returned_for_the_module() -> None:
    form = await _extract(EVELYN)
    d = form.demographics
    assert d.name is not None and d.name.value == "Demo, Evelyn"
    assert d.date_of_birth is not None and d.date_of_birth.value == "04/12/1958"
    assert d.sex is not None and d.sex.value == "F"
    assert d.phone is not None and d.phone.value == "555-0142"
    assert d.name.verification_status is VerificationStatus.VERIFIED_EXACT
    assert form.printed_identity is not None
    assert (form.printed_identity.name, form.printed_identity.dob) == ("Demo, Evelyn", date(1958, 4, 12))


async def test_a_blank_allergy_section_asserts_nothing_and_an_embedded_instruction_is_data() -> None:
    form = await _extract(BLANK)
    assert form.allergies == []
    assert form.allergies_none_stated is None  # never "no known allergies"
    assert [m.name for m in form.current_medications] == ["Amlodipine"]


async def test_verification_is_the_matchers_never_the_models() -> None:
    draft = IntakeDraft(
        current_medications=[
            MedicationDraft(name="Metformin", dose="500 mg", frequency="twice daily", quote="Metformin 500 mg twice daily"),
            MedicationDraft(name="Warfarin", dose="5 mg", frequency="daily", quote="Warfarin 5 mg daily"),
            MedicationDraft(name="Metformin", dose="850 mg", quote="Metformin 850 mg"),
        ],
    )
    form = await _extract(EVELYN, provider=FakeProvider(draft))
    statuses = [(m.name, m.verification_status, m.citation.bbox is not None) for m in form.current_medications]
    assert statuses[0] == ("Metformin", VerificationStatus.VERIFIED_EXACT, True)
    # Not on the page: unverified, no box.
    assert statuses[1] == ("Warfarin", VerificationStatus.UNVERIFIED, False)
    # The name is on the page, the dose is not: the item as reported is not confirmed.
    assert statuses[2] == ("Metformin", VerificationStatus.UNVERIFIED, False)


async def test_an_illegible_entry_is_named_unreadable_and_never_boxed() -> None:
    draft = IntakeDraft(
        current_medications=[MedicationDraft(name=None, dose="500 mg", quote="M#### 500 mg", unreadable=True)],
        allergies=[AllergyDraft(substance="Penicillin", reaction=None, quote="Penicillin - h###", unreadable=True)],
        illegible_fields=["chief_concern"],
    )
    form = await _extract(EVELYN, provider=FakeProvider(draft))
    med, allergy = form.current_medications[0], form.allergies[0]
    assert med.name is None and med.verification_status is VerificationStatus.UNREADABLE and med.citation.bbox is None
    assert allergy.substance == "Penicillin" and allergy.verification_status is VerificationStatus.UNREADABLE
    assert form.chief_concern is not None and form.chief_concern.value is None
    assert form.chief_concern.verification_status is VerificationStatus.UNREADABLE
    assert form.extraction_metadata.unreadable_count == 3


async def test_a_photo_is_verified_from_ocr_words_and_is_unverified_without_them() -> None:
    draft = IntakeDraft(
        chief_concern="Short of breath on stairs",
        family_history=[FamilyHistoryDraft(relation="Father", condition="heart attack at 58", quote="Father: heart attack at 58")],
        allergies_none_text="None known",
    )
    words = [
        Word(text=t, page=1, bbox=(0.27 + 0.06 * i, 0.35, 0.32 + 0.06 * i, 0.37), source="ocr")
        for i, t in enumerate(["Short", "of", "breath", "on", "stairs"])
    ] + [
        Word(text=t, page=1, bbox=(0.26 + 0.07 * i, 0.75, 0.32 + 0.07 * i, 0.77), source="ocr")
        for i, t in enumerate(["Father:", "heart", "attack", "at", "58"])
    ] + [
        Word(text=t, page=1, bbox=(0.26 + 0.07 * i, 0.65, 0.32 + 0.07 * i, 0.67), source="ocr")
        for i, t in enumerate(["None", "known"])
    ]
    seen = await _extract(PHOTO, provider=FakeProvider(draft), ocr=FakeOcr({1: words}), media_type="image/jpeg")
    assert all(i.verification_status in (VerificationStatus.VERIFIED_EXACT, VerificationStatus.VERIFIED_FUZZY)
               for i in intake_items(seen))
    blind = await _extract(PHOTO, provider=FakeProvider(draft), ocr=FakeOcr(), media_type="image/jpeg")
    assert all(i.verification_status is VerificationStatus.UNVERIFIED and i.citation.bbox is None
               for i in intake_items(blind))


async def test_nothing_the_form_says_reaches_a_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    await _extract(EVELYN)
    for secret in ("Evelyn", "Metformin", "Penicillin", "1958", "555-0142", "thirsty"):
        assert secret not in caplog.text


async def test_an_unsupported_media_type_is_refused() -> None:
    with pytest.raises(ValueError):
        await extract_intake_document(document_id=1, document_bytes=b"x", media_type="image/gif", ocr=FakeOcr())
