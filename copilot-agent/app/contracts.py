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
    """Commitment kinds (USER.md UC-01). ``OTHER`` is extracted but never checked."""

    LAB_TEST = "lab_test"
    MEDICATION = "medication"
    OTHER = "other"


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
    ALLERGY = "allergy"


class EvidenceSource(StrEnum):
    """Evidence collections the module attempts to load into a bundle.

    Listed in ``DataQuality.sources_unavailable`` when the read failed, so the
    matcher can distinguish "checked, nothing found" from "could not check".
    """

    LAB_RESULTS = "lab_results"
    LAB_ORDERS = "lab_orders"
    MEDICATIONS = "medications"
    ALLERGIES = "allergies"


class MedicationAction(StrEnum):
    """Explicit medication action stated in the plan (ARCHITECTURE.md section 7)."""

    START = "start"
    STOP = "stop"
    INCREASE = "increase"
    DECREASE = "decrease"
    SWITCH = "switch"
    CONTINUE = "continue"
    UNCLEAR = "unclear"


class MedicationSource(StrEnum):
    """OpenEMR keeps medications in two tables (AUDIT DATA-001); both are carried, never collapsed."""

    PRESCRIPTIONS = "prescriptions"
    LISTS = "lists"


class LabOrderStatus(StrEnum):
    """Order status as recorded (OpenEMR ``ord_status``); ``unknown`` when blank or unmapped."""

    PENDING = "pending"
    ROUTED = "routed"
    COMPLETE = "complete"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


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
    order_id: str | None = Field(default=None, min_length=1, description="The order this result belongs to, e.g. 'procedure_order:12'.")
    test_name: str = Field(min_length=1)
    code: str | None = Field(default=None, min_length=1, description="LOINC (or lab-local) result code as recorded.")
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
    range: str | None = Field(default=None, min_length=1, description="Reference range exactly as recorded; never interpreted here.")
    status: LabResultStatus
    observed_at: AwareDatetime = Field(description="Result timestamp used for the evidence window.")


class LabOrder(StrictModel):
    """One ordered test (a ``procedure_order_code`` row) from the evidence window."""

    order_id: str = Field(min_length=1, description="Source order id, e.g. 'procedure_order:12'.")
    sequence: int = Field(ge=1, description="procedure_order_code.procedure_order_seq (several tests per order).")
    test_name: str = Field(min_length=1, description="procedure_name as recorded.")
    code: str | None = Field(default=None, min_length=1, description="procedure_code as recorded (often a LOINC code).")
    status: LabOrderStatus
    ordered_at: AwareDatetime

    @property
    def record_id(self) -> str:
        return f"{self.order_id}:{self.sequence}"


class MedicationRecord(StrictModel):
    """One medication row from either source, with its status derived per AUDIT DATA-003.

    ``active`` is None when the source fields disagree (``indeterminate``);
    ``status_field``/``status_value`` say which fields decided it. Dates are
    kept as recorded; ``timestamp`` is the one used for the evidence window.
    """

    record_id: str = Field(min_length=1, description="'prescriptions:<id>' or 'lists:<id>'.")
    source_table: MedicationSource
    drug_name: str = Field(min_length=1, description="prescriptions.drug or lists.title, verbatim.")
    rxnorm_code: str | None = Field(default=None, min_length=1)
    dosage_text: str | None = Field(default=None, min_length=1, description="Free-text dose/sig as recorded; never parsed here.")
    active: bool | None = Field(description="Derived status; None = indeterminate (fields disagree).")
    status_field: str = Field(min_length=1, description="Fields the status was derived from, e.g. 'active,end_date'.")
    status_value: str = Field(min_length=1, description="Their values as recorded, e.g. 'active=1,end_date=null'.")
    started_at: AwareDatetime | None = Field(default=None, description="start_date/date_added (prescriptions) or begdate (lists).")
    ended_at: AwareDatetime | None = Field(default=None, description="end_date (prescriptions) or enddate (lists).")
    modified_at: AwareDatetime | None = Field(default=None, description="prescriptions.date_modified; lists.date.")
    timestamp: AwareDatetime = Field(description="Window timestamp: max(date_added, date_modified) or lists.date.")
    timestamp_field: str = Field(min_length=1)


class AllergyRecord(StrictModel):
    """One allergy entry as recorded (AUDIT DATA-002): uncoded stays uncoded; absence is never inferred."""

    record_id: str = Field(min_length=1, description="'lists:<id>'.")
    title: str = Field(min_length=1)
    coded: bool
    code: str | None = Field(default=None, min_length=1)
    reaction: str | None = Field(default=None, min_length=1)
    severity: str | None = Field(default=None, min_length=1)
    active: bool
    begdate: AwareDatetime | None = None
    enddate: AwareDatetime | None = None
    duplicate_count: int = Field(default=1, ge=1)


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
    user_uuid: UUID | None = Field(
        default=None,
        description="Authorized user's uuid; when present, a briefing ticket's `sub` must match it.",
    )
    prior_note: PriorNote
    lab_results: list[LabResult] = Field(default_factory=list)
    lab_orders: list[LabOrder] = Field(default_factory=list)
    medications: list[MedicationRecord] = Field(default_factory=list, description="All medication rows from both sources (not windowed: 'continue' needs older records).")
    allergies: list[AllergyRecord] = Field(default_factory=list, description="Allergy entries as recorded; empty means 'no entries on file', never NKA.")
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
    test_name: str | None = Field(default=None, description="Test name as written, for lab_test commitments.")
    drug_name: str | None = Field(default=None, description="Drug name as written, for medication commitments.")
    action: MedicationAction | None = Field(default=None, description="Stated medication action, for medication commitments.")
    due_text: str | None = Field(default=None, description="Timing as written, e.g. 'in three months'.")
    ambiguity_note: str | None = Field(default=None, description="Model's note that the wording is unclear (e.g. test not named).")


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
    state: EvidenceState | None = Field(
        description="Evidence state assigned by the matcher; None only for kind 'other', which is never checked."
    )
    summary: str = Field(min_length=1, description="Short, non-interpretive description of the evidence.")
    citations: list[Citation] = Field(default_factory=list)
    candidates: list[Citation] = Field(
        default_factory=list,
        description="Records shown side by side for ambiguous or conflicting states; never a claim of a match.",
    )

    @model_validator(mode="after")
    def _state_invariants(self) -> EvidenceMatch:
        if (self.commitment.kind is CommitmentKind.OTHER) != (self.state is None):
            raise ValueError("kind 'other' is the only unchecked kind: it has no state, every other kind has one")
        if self.state in STATES_REQUIRING_CITATIONS and not self.citations:
            raise ValueError(
                f"evidence state '{self.state}' asserts a record exists and requires at least one citation"
            )
        return self


class IntervalAnnotation(StrictModel):
    """Which verified commitment, if any, accounts for one interval record (ARCHITECTURE.md section 7)."""

    record_id: str = Field(min_length=1)
    record_type: RecordType
    explained_by: str | None = Field(default=None, description="commitment_id of the match that cites or lists the record; None = unexplained.")


# --------------------------------------------------------------------------- #
# API request / response
# --------------------------------------------------------------------------- #


class BriefingRequest(StrictModel):
    context: ContextBundle


class BriefingResponse(StrictModel):
    correlation_id: UUID
    patient_uuid: UUID
    matches: list[EvidenceMatch] = Field(default_factory=list)
    interval_annotations: list[IntervalAnnotation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rejected_count: int = Field(default=0, ge=0, description="Model proposals withheld by the verifier (span, name or fabrication checks).")


class HealthResponse(StrictModel):
    status: Literal["ok"]


class DependencyStatus(StrictModel):
    """One readiness dependency (ARCHITECTURE.md section 14, "Endpoints")."""

    status: Literal["ok", "degraded", "unavailable", "not_configured"]
    detail: str | None = Field(default=None, description="Fixed, non-sensitive explanation.")


class ReadyResponse(StrictModel):
    status: Literal["ready", "not_ready"]
    dependencies: dict[str, DependencyStatus]


# --------------------------------------------------------------------------- #
# Hand-off: bundle store and ticket-gated briefing stream
# --------------------------------------------------------------------------- #


class BundleAccepted(StrictModel):
    """Response to ``POST /v1/bundles``: the id the module binds into the ticket."""

    bundle_id: UUID
    correlation_id: UUID
    patient_uuid: UUID
    expires_at: AwareDatetime


class DegradedStage(StrEnum):
    EXTRACTION = "extraction"
    MATCHING = "matching"
    TURN = "turn"


class StreamEnvelope(StrictModel):
    """Every server-sent event carries the binding identifiers (ARCH-002).

    The panel discards any event whose ``patient_uuid`` or ``correlation_id``
    differs from the ones it was rendered with.
    """

    correlation_id: UUID
    patient_uuid: UUID


class CommitmentEvent(StreamEnvelope):
    """One verified commitment with its evidence state (SSE event ``commitment``)."""

    match: EvidenceMatch


class IntervalAnnotationEvent(StreamEnvelope):
    """SSE event ``interval_annotation``: every interval record with its ``explained_by``."""

    annotations: list[IntervalAnnotation] = Field(default_factory=list)


class CompleteEvent(StreamEnvelope):
    """Terminal event (SSE event ``complete``): counts and fixed warnings; never clinical text."""

    commitments: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)
    rejected_count: int = Field(default=0, ge=0, description="Model proposals withheld by the verifier; the panel shows the count.")


class DegradedEvent(StreamEnvelope):
    """Terminal event (SSE event ``degraded``): the plan check could not be produced.

    ``deterministic_sections_intact`` is always true: the module's sections
    were rendered before the agent was contacted and do not depend on it.
    """

    stage: DegradedStage
    reason_code: str = Field(min_length=1)
    deterministic_sections_intact: Literal[True] = True


# --------------------------------------------------------------------------- #
# Follow-up turns (UC-04)
# --------------------------------------------------------------------------- #


class StatementKind(StrEnum):
    """Kinds of statement a follow-up answer may contain (ARCHITECTURE.md section 7)."""

    FACT = "fact"
    NO_RECORD_FOUND = "no_record_found"
    CLARIFICATION = "clarification"
    REFUSAL = "refusal"


# The only two texts a rendered refusal may carry (ARCHITECTURE.md section 9, "Domain constraints"). The prompt asks
# the model for them verbatim; the verifier substitutes one of them for whatever the model wrote, so a refusal never
# carries model prose and can never be used to smuggle advice past the deny-lists.
SCOPE_REFUSAL_TEXT = (
    "This question is outside what the Co-Pilot can check. "
    "It answers only from this patient's results, orders, medications, allergies and the last plan."
)
ADVICE_REFUSAL_TEXT = (
    "The Co-Pilot does not give treatment advice or clinical interpretation. "
    "It reports only what this patient's record shows."
)


class ToolCallRecord(StrictModel):
    """One tool invocation in a turn (logged as a span; returned so the panel can show what was searched)."""

    tool: str = Field(min_length=1)
    args: dict[str, str | int | bool | None] = Field(default_factory=dict)
    records: int = Field(ge=0)
    truncated: bool = False
    error: str | None = Field(default=None, description="Fixed, non-clinical error code when the tool failed.")


class VerifiedStatement(StrictModel):
    """A statement that survived the verifier, with the records it is attributed to."""

    text: str = Field(min_length=1)
    kind: StatementKind
    citations: list[Citation] = Field(default_factory=list)


class TurnRequest(StrictModel):
    question: str = Field(min_length=1, max_length=1000, description="Physician's typed question about the bound patient.")


class VerifiedTurn(StreamEnvelope):
    """Agent -> panel: verified statements only, plus what was withheld and searched."""

    turn_index: int = Field(ge=1)
    statements: list[VerifiedStatement] = Field(default_factory=list)
    rejected_count: int = Field(default=0, ge=0)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    degraded: DegradedEvent | None = Field(default=None, description="Set when the turn could not be produced; statements is then empty.")


class ErrorDetail(StrictModel):
    """Structured, non-clinical error body for a briefing that could not be produced.

    Returned (wrapped as ``{"detail": ...}``) instead of a briefing so that a
    provider outage is never mistaken for "no commitments found".
    """

    code: str = Field(min_length=1, description="Stable machine-readable category, e.g. 'provider_unavailable'.")
    message: str = Field(min_length=1, description="Fixed, non-clinical explanation.")
    correlation_id: UUID | None = None
    patient_uuid: UUID | None = None


__all__ = [
    "SCHEMA_VERSION",
    "STATES_REQUIRING_CITATIONS",
    "StatementKind",
    "ToolCallRecord",
    "TurnRequest",
    "VerifiedStatement",
    "VerifiedTurn",
    "AbnormalFlag",
    "AllergyRecord",
    "BriefingRequest",
    "BriefingResponse",
    "BundleAccepted",
    "Citation",
    "CommitmentEvent",
    "CommitmentKind",
    "CompleteEvent",
    "ContextBundle",
    "DataQuality",
    "DegradedEvent",
    "DegradedStage",
    "DependencyStatus",
    "ErrorDetail",
    "EvidenceMatch",
    "EvidenceSource",
    "EvidenceState",
    "ExtractedCommitment",
    "ExtractionOutput",
    "HealthResponse",
    "IntervalAnnotation",
    "IntervalAnnotationEvent",
    "LabOrder",
    "LabOrderStatus",
    "LabResult",
    "LabResultStatus",
    "MedicationAction",
    "MedicationRecord",
    "MedicationSource",
    "PriorNote",
    "ReadyResponse",
    "RecordType",
    "StreamEnvelope",
    "StrictModel",
]
