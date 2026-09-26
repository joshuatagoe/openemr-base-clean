"""Intake-form extraction pipeline (PRD CR1/CR2, feature PRD F04, ADR-010, ADR-007).

    form bytes (PDF, PNG or JPEG)
        -> ModelProvider.parse_structured(schema=IntakeDraft)   flat, strings only
        -> IntakeDraft (validated, still untrusted)
        -> the ADR-007 matcher: every written value looked up in the page's own
           words (PDF text layer, or Textract OCR for photos and handwriting)
        -> IntakeForm (public contract, app.intake)

The model reads; it never decides verification, boxes, citation identity or
what an empty section means. Those are ours:

- **Verification** comes only from :func:`app.verification.verify_document`,
  the same matcher and OCR policy the lab path uses. An item is verified only
  when every part the model reported (name, dose, frequency...) is found on one
  page; its box is the union of their boxes. Anything else is ``unverified``
  with no box; an entry the model marked illegible is ``unreadable``, no box.
- **Anchors.** A secondary part (a dose, a reaction, a condition) must be on the
  same line as its entry's main text, so "daily" is Lisinopril's "daily" and not
  Metformin's. Main texts are looked up unanchored: a value written twice is
  ambiguous, and ambiguous is unverified.
- **Absence.** A blank section stays an empty list with no "none" statement.

Logs and spans carry counts only - never a name, a medication or a quote.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import Field

from app.contracts import StrictModel
from app.documents import (
    SUPPORTED_MEDIA_TYPES,
    DocumentCitation,
    ExtractionMetadata,
    LabResult,
    PrintedIdentity,
    VerificationStatus,
)
from app.intake import (
    Demographics,
    FamilyHistoryItem,
    IntakeField,
    IntakeForm,
    ReportedAllergy,
    ReportedMedication,
    intake_items,
)
from app.lab_extractor import _printed_lines
from app.observability import generation, log_event, span
from app.page_text import OcrSource, configured_render_dpi, default_ocr_source
from app.providers.base import ContentPart, DocumentPart, ModelProvider, TextPart
from app.providers.prompt import (
    INTAKE_EXTRACTION_PROMPT_VERSION,
    INTAKE_EXTRACTION_SYSTEM_PROMPT,
    build_intake_document_content,
)
from app.providers.stub_provider import StubProvider
from app.verification import verify_document

INTAKE_MAX_OUTPUT_TOKENS = 4096

#: Longest written phrase the matcher looks for as one run of words (a chief
#: concern, "heart attack at 58"). The lab path keeps the matcher's default (4).
INTAKE_MAX_SPAN_WORDS = 12

_VERIFIED = (VerificationStatus.VERIFIED_EXACT, VerificationStatus.VERIFIED_FUZZY)


# --------------------------------------------------------------------------- #
# What we ask the MODEL for: strings, ints, bools. Mapped to IntakeForm in code.
# --------------------------------------------------------------------------- #

IllegibleField = Literal["patient_name", "patient_dob", "patient_sex", "patient_phone", "chief_concern"]


class MedicationDraft(StrictModel):
    name: str | None = Field(default=None, description="As written. Null only when illegible (then unreadable=true).")
    dose: str | None = None
    frequency: str | None = None
    quote: str = Field(min_length=1, description="The whole entry exactly as written.")
    page: int = Field(default=1, ge=1)
    unreadable: bool = Field(default=False, description="True when any part of the entry cannot be read. Never guess it.")


class AllergyDraft(StrictModel):
    substance: str | None = Field(default=None, description="As written. Null only when illegible.")
    reaction: str | None = None
    quote: str = Field(min_length=1)
    page: int = Field(default=1, ge=1)
    unreadable: bool = False


class FamilyHistoryDraft(StrictModel):
    relation: str | None = None
    condition: str | None = Field(default=None, description="As written. Null only when illegible.")
    quote: str = Field(min_length=1)
    page: int = Field(default=1, ge=1)
    unreadable: bool = False


class IntakeDraft(StrictModel):
    """Flat reading of the form the model returns."""

    patient_name: str | None = Field(default=None, description="Exactly as written, or null.")
    patient_dob_as_written: str | None = Field(default=None, description="The date of birth exactly as written.")
    patient_dob: str | None = Field(default=None, description="The same date as YYYY-MM-DD when unambiguous, else null.")
    patient_sex: str | None = None
    patient_phone: str | None = None
    chief_concern: str | None = Field(default=None, description="The reason for the visit exactly as written.")
    illegible_fields: list[IllegibleField] = Field(default_factory=list)
    current_medications: list[MedicationDraft] = Field(default_factory=list)
    medications_none_text: str | None = Field(default=None, description='Only a written "none"; blank is null.')
    allergies: list[AllergyDraft] = Field(default_factory=list)
    allergies_none_text: str | None = Field(
        default=None, description='Only a written "None"/"NKDA"/"No known allergies". A blank section is null.'
    )
    family_history: list[FamilyHistoryDraft] = Field(default_factory=list)
    family_history_none_text: str | None = None
    page_count: int = Field(default=1, ge=1)


# --------------------------------------------------------------------------- #
# Verification: probes through the one matcher
# --------------------------------------------------------------------------- #

# The matcher is written for lab rows: it looks a value up on the row "named" by
# a test name. Each written value becomes such a probe - the text to find, and
# the words that must share its line (the anchor). No anchor: a name with no
# word of two letters, so the matcher applies no row rule.
_NO_ANCHOR = "-"


@dataclass(frozen=True)
class _Probe:
    text: str
    anchor: str
    page: int | None


@dataclass(frozen=True)
class _Found:
    status: VerificationStatus
    page: int | None
    bbox: tuple[float, float, float, float] | None


def _clean(text: str | None) -> str | None:
    return text.strip() if text and text.strip() else None


def _probe_result(probe: _Probe, document_id: int) -> LabResult:
    return LabResult(
        test_name=probe.anchor or _NO_ANCHOR,
        value=probe.text,
        verification_status=VerificationStatus.UNVERIFIED,
        citation=DocumentCitation(
            source_id=str(document_id), page_or_section="p. 1", field_or_chunk_id="probe", quote_or_value=probe.text
        ),
    )


def _combine(found: list[LabResult]) -> _Found:
    """All parts found on one page -> verified (exact only if all exact), box = their union."""
    if not found or any(r.verification_status not in _VERIFIED for r in found):
        return _Found(VerificationStatus.UNVERIFIED, None, None)
    pages = {r.citation.page for r in found}
    if len(pages) != 1:
        return _Found(VerificationStatus.UNVERIFIED, None, None)
    boxes = [r.citation.bbox for r in found if r.citation.bbox is not None]
    box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    exact = all(r.verification_status is VerificationStatus.VERIFIED_EXACT for r in found)
    return _Found(VerificationStatus.VERIFIED_EXACT if exact else VerificationStatus.VERIFIED_FUZZY, pages.pop(), box)


class _Verifier:
    """Collects every probe, runs the matcher once over the document, answers per group."""

    def __init__(self, document_id: int) -> None:
        self.document_id = document_id
        self.probes: list[_Probe] = []
        # group key -> list of alternatives, each a list of probe indices (first alternative found wins)
        self.groups: dict[str, list[list[int]]] = {}

    def add(self, key: str, *alternatives: list[_Probe]) -> None:
        alts: list[list[int]] = []
        for probes in alternatives:
            idx = []
            for p in probes:
                idx.append(len(self.probes))
                self.probes.append(p)
            alts.append(idx)
        self.groups[key] = alts

    async def run(self, *, document: bytes, media_type: str, ocr: OcrSource) -> tuple[dict[str, _Found], int, object]:
        results, stats = await verify_document(
            [_probe_result(p, self.document_id) for p in self.probes],
            [p.page for p in self.probes],
            document=document,
            media_type=media_type,
            ocr=ocr,
            dpi=configured_render_dpi(),
            max_words=INTAKE_MAX_SPAN_WORDS,
        )
        answers: dict[str, _Found] = {}
        for key, alts in self.groups.items():
            chosen = _Found(VerificationStatus.UNVERIFIED, None, None)
            for idx in alts:
                combined = _combine([results[i] for i in idx])
                if combined.status in _VERIFIED:
                    chosen = combined
                    break
            answers[key] = chosen
        return answers, stats.page_count, stats


def _cite(document_id: int, field: str, quote: str, page_hint: int, found: _Found | None) -> DocumentCitation:
    page = found.page if found is not None and found.page is not None else page_hint
    return DocumentCitation(
        source_id=str(document_id),
        page_or_section=f"p. {page}",
        field_or_chunk_id=field,
        quote_or_value=quote,
        page=found.page if found is not None else None,
        bbox=found.bbox if found is not None else None,
    )


# --------------------------------------------------------------------------- #
# Draft -> IntakeForm
# --------------------------------------------------------------------------- #

_FIELD_ANCHORS = {"patient_sex": "sex gender"}
_NONE_ANCHORS = {
    "medications_none": "medications medication",
    "allergies_none": "allergies allergy",
    "family_history_none": "family history",
}


def _as_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _single_fields(draft: IntakeDraft) -> dict[str, str | None]:
    return {
        "patient_name": _clean(draft.patient_name),
        "patient_dob": _clean(draft.patient_dob_as_written),
        "patient_sex": _clean(draft.patient_sex),
        "patient_phone": _clean(draft.patient_phone),
        "chief_concern": _clean(draft.chief_concern),
        "medications_none": _clean(draft.medications_none_text),
        "allergies_none": _clean(draft.allergies_none_text),
        "family_history_none": _clean(draft.family_history_none_text),
    }


def _plan(draft: IntakeDraft, document_id: int) -> _Verifier:
    v = _Verifier(document_id)
    for key, text in _single_fields(draft).items():
        if text is None or key in draft.illegible_fields:
            continue
        anchor = _FIELD_ANCHORS.get(key) or _NONE_ANCHORS.get(key)
        plain = [_Probe(text, _NO_ANCHOR, None)]
        v.add(key, *([[_Probe(text, anchor, None)], plain] if anchor else [plain]))

    def item(key: str, primary: str | None, secondary: list[str | None], page: int, unreadable: bool) -> None:
        if unreadable or primary is None:
            return
        probes = [_Probe(primary, _NO_ANCHOR, page)] + [_Probe(s, primary, page) for s in secondary if s is not None]
        v.add(key, probes)

    for i, m in enumerate(draft.current_medications):
        item(f"med{i}", _clean(m.name), [_clean(m.dose), _clean(m.frequency)], m.page, m.unreadable)
    for i, a in enumerate(draft.allergies):
        item(f"allergy{i}", _clean(a.substance), [_clean(a.reaction)], a.page, a.unreadable)
    for i, f in enumerate(draft.family_history):
        condition, relation = _clean(f.condition), _clean(f.relation)
        # The relation is anchored to the condition's line, like a dose to its drug.
        item(f"family{i}", condition, [relation], f.page, f.unreadable)
    return v


def _field(document_id: int, key: str, field: str, draft: IntakeDraft, found: dict[str, _Found]) -> IntakeField | None:
    text = _single_fields(draft)[key]
    if key in draft.illegible_fields:
        return IntakeField(
            value=None,
            verification_status=VerificationStatus.UNREADABLE,
            citation=_cite(document_id, field, text or "[illegible]", 1, None),
        )
    if text is None:
        return None
    hit = found.get(key)
    return IntakeField(
        value=text,
        verification_status=hit.status if hit else VerificationStatus.UNVERIFIED,
        citation=_cite(document_id, field, text, 1, hit),
    )


def _item_state(key: str, unreadable: bool, primary: str | None, found: dict[str, _Found]) -> tuple[VerificationStatus, _Found | None]:
    # A legible entry without its main text is the model contradicting itself; it is
    # treated as illegible - never completed.
    if unreadable or primary is None:
        return VerificationStatus.UNREADABLE, None
    hit = found.get(key)
    if hit is None:
        return VerificationStatus.UNVERIFIED, None
    return hit.status, hit


def draft_to_form(
    draft: IntakeDraft, document_id: int, found: dict[str, _Found], *, model_id: str, page_count: int
) -> IntakeForm:
    meds = []
    for i, m in enumerate(draft.current_medications):
        status, hit = _item_state(f"med{i}", m.unreadable, _clean(m.name), found)
        meds.append(ReportedMedication(
            name=_clean(m.name), dose=_clean(m.dose), frequency=_clean(m.frequency), verification_status=status,
            citation=_cite(document_id, f"current_medications[{i}]", m.quote, m.page, hit),
        ))
    allergies = []
    for i, a in enumerate(draft.allergies):
        status, hit = _item_state(f"allergy{i}", a.unreadable, _clean(a.substance), found)
        allergies.append(ReportedAllergy(
            substance=_clean(a.substance), reaction=_clean(a.reaction), verification_status=status,
            citation=_cite(document_id, f"allergies[{i}]", a.quote, a.page, hit),
        ))
    family = []
    for i, f in enumerate(draft.family_history):
        status, hit = _item_state(f"family{i}", f.unreadable, _clean(f.condition), found)
        family.append(FamilyHistoryItem(
            relation=_clean(f.relation), condition=_clean(f.condition), verification_status=status,
            citation=_cite(document_id, f"family_history[{i}]", f.quote, f.page, hit),
        ))

    def fld(key: str, field: str) -> IntakeField | None:
        return _field(document_id, key, field, draft, found)

    demographics = Demographics(
        name=fld("patient_name", "demographics.name"),
        date_of_birth=fld("patient_dob", "demographics.date_of_birth"),
        sex=fld("patient_sex", "demographics.sex"),
        phone=fld("patient_phone", "demographics.phone"),
    )
    form = IntakeForm(
        document_id=document_id,
        demographics=demographics,
        chief_concern=fld("chief_concern", "chief_concern"),
        current_medications=meds,
        medications_none_stated=fld("medications_none", "medications_none_stated"),
        allergies=allergies,
        allergies_none_stated=fld("allergies_none", "allergies_none_stated"),
        family_history=family,
        family_history_none_stated=fld("family_history_none", "family_history_none_stated"),
        extraction_metadata=_placeholder_metadata(model_id),
        printed_identity=_printed_identity(draft),
    )
    return form.model_copy(update={"extraction_metadata": summarize_form(form, model_id=model_id, page_count=page_count)})


def _placeholder_metadata(model_id: str) -> ExtractionMetadata:
    return ExtractionMetadata(
        model_id=model_id, prompt_version=INTAKE_EXTRACTION_PROMPT_VERSION, extracted_at=datetime.now(UTC),
        page_count=1, verified_fraction=0.0, unreadable_count=0, unverified_count=0,
    )


def summarize_form(form: IntakeForm, *, model_id: str, page_count: int) -> ExtractionMetadata:
    """Counts over every item and demographic field, recomputed so they cannot disagree with the form."""
    d = form.demographics
    statuses = [f.verification_status for f in (d.name, d.date_of_birth, d.sex, d.phone) if f is not None]
    statuses += [i.verification_status for i in intake_items(form)]
    verified = sum(1 for s in statuses if s in _VERIFIED)
    return ExtractionMetadata(
        model_id=model_id,
        prompt_version=INTAKE_EXTRACTION_PROMPT_VERSION,
        extracted_at=datetime.now(UTC),
        page_count=max(1, page_count),
        verified_fraction=(verified / len(statuses)) if statuses else 0.0,
        unreadable_count=sum(1 for s in statuses if s is VerificationStatus.UNREADABLE),
        unverified_count=sum(1 for s in statuses if s is VerificationStatus.UNVERIFIED),
    )


def _printed_identity(draft: IntakeDraft) -> PrintedIdentity | None:
    """Name and DOB as written, for the module's check (ADR-012). An illegible one is absent, never guessed."""
    name = None if "patient_name" in draft.illegible_fields else _clean(draft.patient_name)
    dob = None if "patient_dob" in draft.illegible_fields else _as_date(draft.patient_dob)
    if name is None and dob is None:
        return None
    return PrintedIdentity(name=name, dob=dob)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def extract_intake_document(
    *,
    document_id: int,
    document_bytes: bytes,
    media_type: str,
    provider: ModelProvider | None = None,
    ocr: OcrSource | None = None,
) -> IntakeForm:
    """Extract one stored intake form. ``provider`` defaults to the offline stub.

    Raises ``ProviderError`` when the model call fails, ``ValueError`` for an
    unsupported or empty document. OCR failure only leaves values unverified.
    """
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ValueError("unsupported document media type")
    if not document_bytes:
        raise ValueError("document is empty")
    model = provider if provider is not None else StubProvider()
    content: list[ContentPart] = [
        TextPart(text=build_intake_document_content(document_id)),
        DocumentPart(media_type=media_type, data_base64=base64.b64encode(document_bytes).decode("ascii")),
    ]
    log_event("intake_extraction.start", document_id=document_id, media_type=media_type,
              byte_count=len(document_bytes), provider=model.name)
    with generation("intake_extract") as gen:
        parsed = await model.parse_structured(
            system=INTAKE_EXTRACTION_SYSTEM_PROMPT, content=content, schema=IntakeDraft, max_tokens=INTAKE_MAX_OUTPUT_TOKENS
        )
        gen["usage"] = parsed.usage
    draft = parsed.output

    verifier = _plan(draft, document_id)
    with span("verify_document") as attrs:
        found, page_count, stats = await verifier.run(
            document=document_bytes, media_type=media_type, ocr=ocr if ocr is not None else default_ocr_source()
        )
        attrs.update(page_count=page_count, ocr_pages=stats.ocr_pages, ocr_failed_pages=stats.ocr_failed_pages)  # type: ignore[attr-defined]

    form = draft_to_form(draft, document_id, found, model_id=parsed.usage.model, page_count=page_count or draft.page_count)
    meta = form.extraction_metadata
    log_event(
        "intake_extraction.complete",
        document_id=document_id,
        provider=parsed.usage.provider,
        model=parsed.usage.model,
        latency_ms=parsed.usage.latency_ms,
        item_count=len(intake_items(form)),
        unreadable_count=meta.unreadable_count,
        unverified_count=meta.unverified_count,
    )
    return form


# --------------------------------------------------------------------------- #
# StubProvider fixture: reads the typed fixture forms' printed "Label: value" lines
# --------------------------------------------------------------------------- #

_COLUMNS = re.compile(r"\s{2,}")
_US_DATE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def _labelled(column: str, label: str) -> str | None:
    if column.lower().startswith(label.lower() + ":"):
        value = column[len(label) + 1:].strip()
        return None if not value or set(value) <= {"_"} else value
    return None


def _stub_intake_draft(content: list[ContentPart]) -> IntakeDraft:
    documents = [p for p in content if isinstance(p, DocumentPart)]
    if not documents or documents[0].media_type != "application/pdf":
        return IntakeDraft()  # the stub cannot read a photo; a real model or a recording can
    lines = _printed_lines(base64.b64decode(documents[0].data_base64))
    d = IntakeDraft()
    for line in lines:
        cols = [c.strip() for c in _COLUMNS.split(line.strip()) if c.strip()]
        if not cols:
            continue
        head = cols[0]
        for c in cols:
            if (v := _labelled(c, "Name")) is not None:
                d.patient_name = v
            elif (v := _labelled(c, "Date of birth")) is not None:
                d.patient_dob_as_written = v
                if m := _US_DATE.match(v):
                    d.patient_dob = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
            elif (v := _labelled(c, "Sex")) is not None:
                d.patient_sex = v
            elif (v := _labelled(c, "Phone")) is not None:
                d.patient_phone = v
        if (v := _labelled(head, "Chief concern")) is not None:
            d.chief_concern = v
        elif (v := _labelled(head, "Medication")) is not None:
            rest = cols[1:] + [None, None]
            d.current_medications.append(MedicationDraft(name=v, dose=rest[0], frequency=rest[1], quote=line.strip()))
        elif (v := _labelled(head, "Allergy")) is not None:
            reaction = next((_labelled(c, "Reaction") for c in cols[1:] if _labelled(c, "Reaction")), None)
            d.allergies.append(AllergyDraft(substance=v, reaction=reaction, quote=line.strip()))
        elif (v := _labelled(head, "Family history")) is not None:
            d.family_history.append(FamilyHistoryDraft(relation=v, condition=cols[1] if len(cols) > 1 else None, quote=line.strip()))
    return d


StubProvider.register_fixture(IntakeDraft, _stub_intake_draft)


__all__ = [
    "INTAKE_MAX_OUTPUT_TOKENS",
    "INTAKE_MAX_SPAN_WORDS",
    "AllergyDraft",
    "FamilyHistoryDraft",
    "IntakeDraft",
    "MedicationDraft",
    "draft_to_form",
    "extract_intake_document",
    "summarize_form",
]
