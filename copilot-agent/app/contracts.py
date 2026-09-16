"""Canonical Pydantic v2 contracts for the Clinical Co-Pilot agent service.

These models are the source of truth for the agent's request and response
shapes (ARCHITECTURE.md section 7). They are deliberately strict:

* ``extra="forbid"`` on every model, so an unexpected field is a 422, never
  silently accepted.
* Closed sets (commitment kinds, evidence states, result statuses, abnormal
  flags, record types) are enums.
* Timestamps are timezone-aware ``datetime`` values; numeric lab values are
  ``Decimal``.
* No direct patient identifiers (name, DOB, address, phone) exist anywhere in
  these contracts. The patient is referenced by ``patient_uuid`` only.

Evidence states are source-specific. There is no generic "done" state, and
``NO_MATCHING_RECORD_FOUND`` means only that the searched sources contained no
matching record - it is never evidence that a commitment was not carried out.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"


class StrictModel(BaseModel):
    """Base for every contract: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- #
# Closed vocabularies
# --------------------------------------------------------------------------- #


class CommitmentKind(StrEnum):
    """Commitment kinds supported in the initial scope (USERS.md UC-01)."""

    LAB_TEST = "lab_test"
    MEDICATION = "medication"


class EvidenceState(StrEnum):
    """Source-specific evidence states assigned by the deterministic matcher.

    The language model never assigns these. ``NO_MATCHING_RECORD_FOUND`` is
    scoped to the sources searched and must never be rendered or interpreted
    as "not completed". ``VERIFICATION_UNAVAILABLE`` is distinct from it: the
    source could not be checked at all.
    """

    MATCHING_RESULT_FOUND = "matching_result_found"
    ORDER_FOUND_NO_RESULT = "order_found_no_result"
    MATCHING_MEDICATION_RECORD_FOUND = "matching_medication_record_found"
    NO_MATCHING_RECORD_FOUND = "no_matching_record_found"
    AMBIGUOUS_MATCH = "ambiguous_match"
    CONFLICTING_RECORDS = "conflicting_records"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"


# States that assert the existence of a record and therefore require at least
# one citation (ARCHITECTURE.md section 9: no "found" state without a cited
# record).
STATES_REQUIRING_CITATIONS: frozenset[EvidenceState] = frozenset(
    {
        EvidenceState.MATCHING_RESULT_FOUND,
        EvidenceState.ORDER_FOUND_NO_RESULT,
        EvidenceState.MATCHING_MEDICATION_RECORD_FOUND,
        EvidenceState.CONFLICTING_RECORDS,
    }
)


class LabResultStatus(StrEnum):
    """Result status as recorded on the source lab result (AUDIT DATA-005)."""

    PRELIMINARY = "preliminary"
    FINAL = "final"
    CORRECTED = "corrected"
    INCOMPLETE = "incomplete"
    CANNOT_BE_DONE = "cannot_be_done"


class AbnormalFlag(StrEnum):
    """Abnormal flag exactly as the source lab system recorded it.

    This is the only permitted source of interpretation; nothing downstream
    may label a value abnormal unless this flag says so.
    """

    NO = "no"
    YES = "yes"
    HIGH = "high"
    LOW = "low"


class RecordType(StrEnum):
    """Record types a citation may point to."""

    PRIOR_NOTE = "prior_note"
    LAB_RESULT = "lab_result"
    LAB_ORDER = "lab_order"
    MEDICATION = "medication"


class EvidenceSource(StrEnum):
    """Evidence collections the module attempts to load into a bundle.

    Listed in ``DataQuality.sources_unavailable`` when the read failed, so the
    matcher can distinguish "checked, nothing found" from "could not check".
    """

    LAB_RESULTS = "lab_results"


# --------------------------------------------------------------------------- #
# Context bundle (module -> agent)
# --------------------------------------------------------------------------- #


class PriorNote(StrictModel):
    """The baseline encounter note whose plan text is checked for commitments."""

    note_id: str = Field(min_length=1, description="Source record id, e.g. 'form_soap:1001'.")
    encounter_id: str = Field(min_length=1, description="Source encounter id, e.g. 'form_encounter:501'.")
    note_date: AwareDatetime = Field(description="When the note was written (timezone-aware).")
    plan_text: str = Field(
        min_length=1,
        description="Verbatim plan/assessment text. Treated as untrusted input everywhere.",
    )


class LabResult(StrictModel):
    """A single structured lab result from the evidence window."""

    result_id: str = Field(min_length=1, description="Source record id, e.g. 'procedure_result:9001'.")
    test_name: str = Field(min_length=1)
    value: Decimal = Field(description="Numeric result value as recorded.")
    units: str | None = Field(
        default=None,
        min_length=1,
        description="Units as recorded; None when the source left them empty (never '').",
    )
    abnormal_flag: AbnormalFlag | None = Field(
        default=None,
        description="Source abnormal flag; None when the source recorded none.",
    )
    status: LabResultStatus
    observed_at: AwareDatetime = Field(description="Result timestamp used for the evidence window.")


class DataQuality(StrictModel):
    """What the module could and could not load (ARCHITECTURE.md sections 11-12).

    ``sources_unavailable`` names evidence collections whose read failed. A
    source listed here is unreliable for verification even if partial records
    were supplied; the matcher returns ``verification_unavailable`` for
    commitments that depend on it. An absent source is never "no record".
    """

    sources_unavailable: list[EvidenceSource] = Field(default_factory=list)
    duplicates_collapsed: int = Field(
        default=0, ge=0, description="Count of duplicate source rows the module collapsed (AUDIT DATA-004)."
    )


class ContextBundle(StrictModel):
    """Minimum-necessary, single-patient context assembled by the OpenEMR module."""

    schema_version: Literal["1.0"] = Field(
        description="Contract version. Unknown versions are rejected rather than guessed."
    )
    correlation_id: UUID = Field(description="Correlation id minted by the module for this briefing.")
    patient_uuid: UUID = Field(description="OpenEMR patient uuid; the only patient reference allowed.")
    prior_note: PriorNote
    lab_results: list[LabResult] = Field(default_factory=list)
    data_quality: DataQuality = Field(
        default_factory=DataQuality,
        description="Source availability and normalization notes; defaults to 'all sources available'.",
    )


# --------------------------------------------------------------------------- #
# Extraction (model output, before verification)
# --------------------------------------------------------------------------- #


class ExtractedCommitment(StrictModel):
    """A discrete follow-up commitment extracted from the prior plan text.

    ``source_span`` must be verbatim text from the note; the verifier rejects
    any commitment whose span is not found in ``PriorNote.plan_text``.
    """

    commitment_id: str = Field(min_length=1)
    kind: CommitmentKind
    source_span: str = Field(min_length=1, description="Verbatim span from the plan text.")
    test_name: str | None = Field(default=None, description="Normalized test name for lab_test commitments.")
    drug_name: str | None = Field(default=None, description="Normalized drug name for medication commitments.")
    due_text: str | None = Field(default=None, description="Timing as written, e.g. 'in three months'.")


class ExtractionOutput(StrictModel):
    """Structured output of the extraction step."""

    commitments: list[ExtractedCommitment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Evidence (deterministic matcher output)
# --------------------------------------------------------------------------- #


class Citation(StrictModel):
    """Pointer to the source record that supports a claim."""

    record_type: RecordType
    record_id: str = Field(min_length=1)
    timestamp: AwareDatetime


class EvidenceMatch(StrictModel):
    """A commitment paired with the evidence state the matcher assigned."""

    commitment: ExtractedCommitment
    state: EvidenceState
    summary: str = Field(min_length=1, description="Short, non-interpretive description of the evidence.")
    citations: list[Citation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _found_states_require_citations(self) -> EvidenceMatch:
        if self.state in STATES_REQUIRING_CITATIONS and not self.citations:
            raise ValueError(
                f"evidence state '{self.state}' asserts a record exists and requires at least one citation"
            )
        return self


# --------------------------------------------------------------------------- #
# API request / response
# --------------------------------------------------------------------------- #


class BriefingRequest(StrictModel):
    context: ContextBundle


class BriefingResponse(StrictModel):
    correlation_id: UUID
    patient_uuid: UUID
    matches: list[EvidenceMatch] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class HealthResponse(StrictModel):
    status: Literal["ok"]


__all__ = [
    "SCHEMA_VERSION",
    "STATES_REQUIRING_CITATIONS",
    "AbnormalFlag",
    "BriefingRequest",
    "BriefingResponse",
    "Citation",
    "CommitmentKind",
    "ContextBundle",
    "DataQuality",
    "EvidenceMatch",
    "EvidenceSource",
    "EvidenceState",
    "ExtractedCommitment",
    "ExtractionOutput",
    "HealthResponse",
    "LabResult",
    "LabResultStatus",
    "PriorNote",
    "RecordType",
    "StrictModel",
]
