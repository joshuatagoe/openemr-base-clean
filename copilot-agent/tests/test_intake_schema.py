"""The IntakeForm contract (PRD CR2, ADR-010): strict, cited, never guessed."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.documents import DocumentCitation, ExtractionMetadata, VerificationStatus
from app.intake import (
    Demographics,
    FamilyHistoryItem,
    IntakeField,
    IntakeForm,
    ReportedAllergy,
    ReportedMedication,
    intake_items,
)


def _cite(field: str = "current_medications[0]", quote: str = "Metformin 500 mg twice daily", **kw: object) -> DocumentCitation:
    return DocumentCitation(source_id="301", page_or_section="p. 1", field_or_chunk_id=field, quote_or_value=quote, **kw)


def _meta() -> ExtractionMetadata:
    return ExtractionMetadata(
        model_id="m", prompt_version="intake-v1", extracted_at=datetime.now(UTC), page_count=1,
        verified_fraction=0.0, unreadable_count=0, unverified_count=0,
    )


def _form(**over: object) -> IntakeForm:
    data: dict[str, object] = {"document_id": 301, "demographics": Demographics(), "extraction_metadata": _meta()}
    data.update(over)
    return IntakeForm(**data)


def test_a_legible_medication_needs_a_name() -> None:
    with pytest.raises(ValidationError):
        ReportedMedication(name=None, verification_status=VerificationStatus.UNVERIFIED, citation=_cite())


def test_an_unreadable_item_may_lack_its_name_but_never_carries_a_box() -> None:
    item = ReportedMedication(name=None, verification_status=VerificationStatus.UNREADABLE, citation=_cite(quote="M####"))
    assert item.name is None
    with pytest.raises(ValidationError):
        ReportedMedication(
            name=None,
            verification_status=VerificationStatus.UNREADABLE,
            citation=_cite(quote="M####", page=1, bbox=(0.1, 0.1, 0.2, 0.2)),
        )


def test_a_verified_item_must_carry_its_page_and_box() -> None:
    with pytest.raises(ValidationError):
        ReportedAllergy(substance="Penicillin", verification_status=VerificationStatus.VERIFIED_EXACT, citation=_cite())
    ok = ReportedAllergy(
        substance="Penicillin",
        reaction="hives",
        verification_status=VerificationStatus.VERIFIED_EXACT,
        citation=_cite(page=1, bbox=(0.1, 0.1, 0.3, 0.12)),
    )
    assert ok.citation.page == 1


def test_an_unverified_item_has_no_box() -> None:
    with pytest.raises(ValidationError):
        FamilyHistoryItem(
            relation="Mother", condition="type 2 diabetes", verification_status=VerificationStatus.UNVERIFIED,
            citation=_cite(page=1, bbox=(0.1, 0.1, 0.3, 0.12)),
        )


def test_an_unreadable_field_carries_no_value() -> None:
    with pytest.raises(ValidationError):
        IntakeField(value="Headache", verification_status=VerificationStatus.UNREADABLE, citation=_cite())
    with pytest.raises(ValidationError):
        IntakeField(value=None, verification_status=VerificationStatus.UNVERIFIED, citation=_cite())


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        IntakeForm.model_validate({**_form().model_dump(mode="json"), "no_known_allergies": True})


def test_a_blank_allergy_section_is_not_no_known_allergies() -> None:
    """Empty list and no written 'none' statement: nothing is asserted either way."""
    form = _form()
    assert form.allergies == [] and form.allergies_none_stated is None
    assert [i.label for i in intake_items(form)] == []


def test_every_citation_names_the_form() -> None:
    other = _cite().model_copy(update={"source_id": "999"})
    with pytest.raises(ValidationError):
        _form(current_medications=[ReportedMedication(name="Metformin", verification_status=VerificationStatus.UNVERIFIED, citation=other)])


def test_items_flatten_in_a_fixed_order_with_labels() -> None:
    unv = VerificationStatus.UNVERIFIED
    form = _form(
        chief_concern=IntakeField(value="Tired and thirsty", verification_status=unv, citation=_cite("chief_concern", "Tired and thirsty")),
        current_medications=[
            ReportedMedication(name="Metformin", dose="500 mg", frequency="twice daily", verification_status=unv, citation=_cite()),
        ],
        allergies=[ReportedAllergy(substance="Penicillin", reaction="hives", verification_status=unv, citation=_cite("allergies[0]", "Penicillin - hives"))],
        family_history_none_stated=IntakeField(value="None", verification_status=unv, citation=_cite("family_history_none_stated", "None")),
    )
    items = intake_items(form)
    assert [(i.label, i.text) for i in items] == [
        ("Chief concern", "Tired and thirsty"),
        ("Current medication", "Metformin 500 mg twice daily"),
        ("Allergy", "Penicillin; reaction: hives"),
        ("Family history (none reported)", "None"),
    ]
    assert [i.index for i in items] == [0, 1, 2, 3]


def test_an_unreadable_item_text_says_so() -> None:
    form = _form(current_medications=[
        ReportedMedication(name=None, verification_status=VerificationStatus.UNREADABLE, citation=_cite(quote="M####")),
    ])
    (item,) = intake_items(form)
    assert item.text is None and item.verification_status is VerificationStatus.UNREADABLE
