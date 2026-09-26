"""Strict schema for an extracted patient intake form (PRD CR2, feature PRD F04, ADR-010).

An intake form is what the patient *says*: chief concern, the medicines they
report taking, the allergies they report, their family history. It is document
evidence, labelled patient-reported, and never filed into the chart this week
(ADR-010) - the module stores it and refuses to file it (``not_fileable``).

The same three refusals as the lab schema, each an invariant rather than a
convention:

1. **Verification is the matcher's, never the model's.** Every item carries a
   :class:`DocumentCitation`; ``verified_*`` requires the page and box the
   matcher found (ADR-007), ``unverified`` and ``unreadable`` carry no box.
2. **Illegible is named, never guessed.** An unreadable item may lack its main
   text; a legible item may not. An unreadable single field carries no value.
3. **Absence is not a negative.** A blank allergy section is an empty list and
   ``allergies_none_stated=None``: nothing is asserted. Only a patient's own
   written "none" / "NKDA" fills ``allergies_none_stated`` - with its citation.

``intake_items`` is the one flattening of a form into labelled items, in a
fixed order. The OpenEMR module mirrors it (``CandidateMapper::fromIntake``) so
a candidate row's ``result_index`` always points at the same item.

PHI: the name and date of birth are returned to the module once, as
``printed_identity``, for its identity check (ADR-012) and are stripped from the
extraction it stores. Nothing here logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field, model_validator

from app.contracts import StrictModel
from app.documents import DocumentCitation, ExtractionMetadata, PrintedIdentity, VerificationStatus

_VERIFIED = (VerificationStatus.VERIFIED_EXACT, VerificationStatus.VERIFIED_FUZZY)


def _check_citation(status: VerificationStatus, citation: DocumentCitation) -> None:
    if status in _VERIFIED:
        if citation.page is None or citation.bbox is None:
            raise ValueError("a verified item must carry the page and box the matcher found")
    elif citation.bbox is not None:
        raise ValueError("only a verified item carries a box")


class IntakeField(StrictModel):
    """One single-valued field (a demographic, the chief concern), as written."""

    value: str | None = Field(default=None, description="Verbatim as written. None only when unreadable.")
    verification_status: VerificationStatus
    citation: DocumentCitation

    @model_validator(mode="after")
    def _invariants(self) -> IntakeField:
        if self.verification_status is VerificationStatus.UNREADABLE:
            if self.value is not None:
                raise ValueError("an unreadable field must not carry a value")
        elif not (self.value or "").strip():
            raise ValueError("a legible field must carry its value")
        _check_citation(self.verification_status, self.citation)
        return self


class _Item(StrictModel):
    verification_status: VerificationStatus
    citation: DocumentCitation

    def _primary(self) -> str | None:  # pragma: no cover - overridden
        raise NotImplementedError

    @model_validator(mode="after")
    def _invariants(self) -> _Item:
        if self.verification_status is not VerificationStatus.UNREADABLE and not (self._primary() or "").strip():
            raise ValueError("a legible item must carry its main text")
        _check_citation(self.verification_status, self.citation)
        return self


class ReportedMedication(_Item):
    """A medicine the patient reports taking. Patient-reported, never a chart medication."""

    name: str | None = None
    dose: str | None = None
    frequency: str | None = None

    def _primary(self) -> str | None:
        return self.name


class ReportedAllergy(_Item):
    """An allergy the patient reports. Never merged into the chart's allergy list."""

    substance: str | None = None
    reaction: str | None = None

    def _primary(self) -> str | None:
        return self.substance


class FamilyHistoryItem(_Item):
    relation: str | None = None
    condition: str | None = None

    def _primary(self) -> str | None:
        return self.condition


class Demographics(StrictModel):
    """CR2 group 1 (W2-AMB-043 field set). Each field None when not written."""

    name: IntakeField | None = None
    date_of_birth: IntakeField | None = None
    sex: IntakeField | None = None
    phone: IntakeField | None = None


class IntakeForm(StrictModel):
    """A whole extracted intake form. Every section may be empty; empty asserts nothing."""

    document_id: int
    doc_type: Literal["intake_form"] = "intake_form"
    demographics: Demographics
    chief_concern: IntakeField | None = None
    current_medications: list[ReportedMedication] = Field(default_factory=list)
    medications_none_stated: IntakeField | None = Field(
        default=None, description='The patient\'s own written "none" for medications, if any.'
    )
    allergies: list[ReportedAllergy] = Field(default_factory=list)
    allergies_none_stated: IntakeField | None = Field(
        default=None,
        description='The patient\'s own written "none"/"NKDA". A blank section is None here, never "no known allergies".',
    )
    family_history: list[FamilyHistoryItem] = Field(default_factory=list)
    family_history_none_stated: IntakeField | None = None
    extraction_metadata: ExtractionMetadata
    printed_identity: PrintedIdentity | None = Field(
        default=None, description="Name and DOB as written, for the module's identity check only. Never logged."
    )

    @model_validator(mode="after")
    def _attributed(self) -> IntakeForm:
        if any(c.source_id != str(self.document_id) for c in all_citations(self)):
            raise ValueError("every citation must name this form's document_id")
        return self


def all_citations(form: IntakeForm) -> list[DocumentCitation]:
    fields = [form.demographics.name, form.demographics.date_of_birth, form.demographics.sex, form.demographics.phone]
    out = [f.citation for f in fields if f is not None]
    out += [i.citation for i in intake_items(form)]
    return out


# --------------------------------------------------------------------------- #
# The one flattening (mirrored by the module's CandidateMapper)
# --------------------------------------------------------------------------- #

Section = Literal["chief_concern", "medication", "allergy", "family_history"]

LABEL_CHIEF_CONCERN = "Chief concern"
LABEL_MEDICATION = "Current medication"
LABEL_MEDICATIONS_NONE = "Current medications (none reported)"
LABEL_ALLERGY = "Allergy"
LABEL_ALLERGIES_NONE = "Allergies (none reported)"
LABEL_FAMILY_HISTORY = "Family history"
LABEL_FAMILY_HISTORY_NONE = "Family history (none reported)"


@dataclass(frozen=True)
class IntakeItem:
    index: int
    section: Section
    label: str
    text: str | None  # None when unreadable and nothing legible remains
    verification_status: VerificationStatus
    citation: DocumentCitation


def _join(*parts: str | None, sep: str = " ") -> str | None:
    kept = [p.strip() for p in parts if p and p.strip()]
    return sep.join(kept) or None


def medication_text(m: ReportedMedication) -> str | None:
    return _join(m.name, m.dose, m.frequency)


def allergy_text(a: ReportedAllergy) -> str | None:
    if a.reaction and a.reaction.strip():
        return _join(a.substance, f"reaction: {a.reaction.strip()}", sep="; ")
    return _join(a.substance)


def family_history_text(f: FamilyHistoryItem) -> str | None:
    if f.relation and f.relation.strip() and f.condition and f.condition.strip():
        return f"{f.relation.strip()}: {f.condition.strip()}"
    return _join(f.relation, f.condition)


def intake_items(form: IntakeForm) -> list[IntakeItem]:
    """Every reported item, in the fixed order: chief concern, medications, allergies, family history.

    Each "none stated" follows its section's items. Demographics are not items:
    the name and DOB are an identity check, not clinical evidence.
    """
    raw: list[tuple[Section, str, str | None, VerificationStatus, DocumentCitation]] = []
    if form.chief_concern is not None:
        f = form.chief_concern
        raw.append(("chief_concern", LABEL_CHIEF_CONCERN, f.value, f.verification_status, f.citation))
    for m in form.current_medications:
        raw.append(("medication", LABEL_MEDICATION, medication_text(m), m.verification_status, m.citation))
    if form.medications_none_stated is not None:
        f = form.medications_none_stated
        raw.append(("medication", LABEL_MEDICATIONS_NONE, f.value, f.verification_status, f.citation))
    for a in form.allergies:
        raw.append(("allergy", LABEL_ALLERGY, allergy_text(a), a.verification_status, a.citation))
    if form.allergies_none_stated is not None:
        f = form.allergies_none_stated
        raw.append(("allergy", LABEL_ALLERGIES_NONE, f.value, f.verification_status, f.citation))
    for h in form.family_history:
        raw.append(("family_history", LABEL_FAMILY_HISTORY, family_history_text(h), h.verification_status, h.citation))
    if form.family_history_none_stated is not None:
        f = form.family_history_none_stated
        raw.append(("family_history", LABEL_FAMILY_HISTORY_NONE, f.value, f.verification_status, f.citation))
    return [IntakeItem(i, s, label, text, status, cite) for i, (s, label, text, status, cite) in enumerate(raw)]


__all__ = [
    "Demographics",
    "FamilyHistoryItem",
    "IntakeField",
    "IntakeForm",
    "IntakeItem",
    "LABEL_ALLERGIES_NONE",
    "LABEL_ALLERGY",
    "LABEL_CHIEF_CONCERN",
    "LABEL_FAMILY_HISTORY",
    "LABEL_FAMILY_HISTORY_NONE",
    "LABEL_MEDICATION",
    "LABEL_MEDICATIONS_NONE",
    "ReportedAllergy",
    "ReportedMedication",
    "all_citations",
    "allergy_text",
    "family_history_text",
    "intake_items",
    "medication_text",
]
