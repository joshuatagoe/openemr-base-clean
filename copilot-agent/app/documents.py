"""Strict schemas for extracted clinical documents (PRD CR2, feature PRD F03).

The model proposes; these schemas dispose. Every field the PRD names as required
is here, and `extra="forbid"` means a model that invents a field fails validation
rather than smuggling it downstream.

Two fields exist because a scanned document can lie in ways a note cannot:

``abnormal_flag_source`` distinguishes a flag **printed on the report** from one
**we computed** by comparing value to range. Presenting a computed comparison as
a lab-printed flag is the single most consequential display error in this system
(W2-AMB-055), so provenance is carried in the data rather than reconstructed in
the UI.

``verification_status`` records whether the extracted text was found verbatim in
the document. An unreadable region must be nameable as unreadable - never guessed
and never silently dropped (W2-AMB-010).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, get_args

from pydantic import Field, model_validator

from app.contracts import StrictModel

#: The one media-type allow-list (ADR-007 addition 3). The extractor, the routes
#: and the OCR source import it; the OpenEMR reader normalises to the same three
#: (a test compares them). GIF and WEBP are excluded: Textract cannot read them,
#: so a value extracted from one could never be verified against the page.
MediaType = Literal["application/pdf", "image/png", "image/jpeg"]
SUPPORTED_MEDIA_TYPES: frozenset[str] = frozenset(get_args(MediaType))


class AbnormalFlag(StrEnum):
    """Flags as printed on a US lab report."""

    HIGH = "H"
    LOW = "L"
    CRITICAL_HIGH = "HH"
    CRITICAL_LOW = "LL"
    ABNORMAL = "A"
    NORMAL = "N"


class AbnormalFlagSource(StrEnum):
    EXTRACTED = "extracted"      # printed on the document
    DERIVED = "derived"          # computed from value vs reference_range
    UNAVAILABLE = "unavailable"  # neither printed nor computable


class VerificationStatus(StrEnum):
    VERIFIED_EXACT = "verified_exact"
    VERIFIED_FUZZY = "verified_fuzzy"
    UNVERIFIED = "unverified"
    UNREADABLE = "unreadable"


class DocumentCitation(StrictModel):
    """CR5's minimum citation shape, narrowed to document sources.

    The full cross-source citation type is owned by F05; this is the subset a
    lab extraction produces, kept here so F03 can land without waiting on it.
    """

    source_type: Literal["document"] = "document"
    source_id: str = Field(min_length=1, description="OpenEMR documents.id for the stored source file.")
    page_or_section: str = Field(min_length=1, description='Human-readable locator, e.g. "p. 2".')
    field_or_chunk_id: str = Field(min_length=1, description="Which field of the extraction this supports.")
    quote_or_value: str = Field(min_length=1, description="Verbatim text as printed. Never paraphrased.")
    page: int | None = Field(default=None, ge=1, description="1-indexed page for the bbox.")
    bbox: tuple[float, float, float, float] | None = Field(
        default=None,
        description="Normalised 0..1 (x0, y0, x1, y1), top-left origin. Null when not localisable.",
    )

    @model_validator(mode="after")
    def _check_bbox(self) -> DocumentCitation:
        if self.bbox is None:
            return self
        if self.page is None:
            raise ValueError("bbox requires a page")
        x0, y0, x1, y1 = self.bbox
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            # A validation error, never a clamp: silently corrected coordinates
            # would draw a highlight over the wrong text, which is worse than none.
            raise ValueError("bbox coordinates must satisfy 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")
        return self


class LabResult(StrictModel):
    """One test result as printed (CR2's seven required fields)."""

    test_name: str = Field(min_length=1)
    value: Decimal | str | None = Field(default=None, description="str permits non-numeric results such as 'Negative'.")
    unit: str | None = None
    reference_range: str | None = Field(default=None, description="Exactly as printed, e.g. '4.0-5.6'.")
    collection_date: date | None = None
    abnormal_flag: AbnormalFlag | None = None
    abnormal_flag_source: AbnormalFlagSource = AbnormalFlagSource.UNAVAILABLE
    citation: DocumentCitation
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    loinc_code: str | None = None

    @model_validator(mode="after")
    def _invariants(self) -> LabResult:
        if isinstance(self.value, Decimal) and self.unit is None:
            raise ValueError("a numeric value requires a unit")
        if self.abnormal_flag_source is AbnormalFlagSource.DERIVED and self.reference_range is None:
            raise ValueError("a derived abnormal flag requires the reference range it was derived from")
        if self.abnormal_flag is not None and self.abnormal_flag_source is AbnormalFlagSource.UNAVAILABLE:
            raise ValueError("an abnormal flag must record whether it was printed or derived")
        if self.verification_status is VerificationStatus.UNREADABLE and self.value is not None:
            raise ValueError("an unreadable result must not carry a value")
        return self


class ExtractionMetadata(StrictModel):
    model_id: str
    prompt_version: str
    extracted_at: datetime
    page_count: int = Field(ge=1)
    verified_fraction: float = Field(ge=0.0, le=1.0)
    unreadable_count: int = Field(ge=0)
    unverified_count: int = Field(ge=0)


class PrintedIdentity(StrictModel):
    """The patient name and date of birth printed on the report (ADR-012).

    Returned to the OpenEMR module only, which compares it with the chart and
    holds a mismatch back. PHI: never logged, never traced.
    """

    name: str | None = Field(default=None, description="As printed; never normalised.")
    dob: date | None = None


class LabDocument(StrictModel):
    """A whole extracted lab report. Zero results is valid - and is not a failure."""

    document_id: int
    doc_type: Literal["lab_pdf"] = "lab_pdf"
    collection_date: date | None = Field(default=None, description="Document-level; a per-result date wins.")
    ordering_provider: str | None = Field(
        default=None,
        description="The provider printed on the report. Never the clinician who verifies it (F06).",
    )
    results: list[LabResult] = Field(default_factory=list)
    extraction_metadata: ExtractionMetadata
    printed_identity: PrintedIdentity | None = Field(
        default=None,
        description="Name and DOB printed on the report, for the module's identity check. Never logged.",
    )
