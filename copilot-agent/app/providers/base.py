"""ModelProvider port: the only place the agent talks to a language model.

A provider receives the minimum-necessary text (``PriorNote.plan_text`` and
nothing else), returns a structured, *untrusted* ``ModelExtractionOutput``
plus usage metadata, and raises one of the ``Provider*`` exceptions below on
failure. It knows nothing about OpenEMR, HTTP endpoints, evidence matching or
patient identity (ARCHITECTURE.md section 8, "Replaceable model interface").

The model never produces commitment ids or evidence states; those are
assigned in application code after validation and span verification.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from app.contracts import CommitmentKind, MedicationAction, StatementKind, StrictModel

# --------------------------------------------------------------------------- #
# Model-facing (internal, untrusted until verified) contracts
# --------------------------------------------------------------------------- #


class ModelCommitment(StrictModel):
    """One explicit follow-up commitment as proposed by the model.

    Field descriptions double as the JSON-schema guidance the model sees.
    """

    kind: CommitmentKind = Field(description="lab_test for a test/lab to be obtained; medication for a start/stop/continue/change of a drug; other for any other explicit plan action (referral, imaging, follow-up visit, counseling).")
    source_span: str = Field(
        min_length=1,
        description="The exact, verbatim sentence or fragment from the plan that states this commitment. Copy it character for character; do not paraphrase.",
    )
    test_name: str | None = Field(
        default=None,
        description="For lab_test only: the test name exactly as written in the span (do not expand or normalize abbreviations).",
    )
    drug_name: str | None = Field(
        default=None,
        description="For medication only: the drug name exactly as written in the span, without dose or frequency.",
    )
    action: MedicationAction | None = Field(
        default=None,
        description="For medication only: the action the plan states - start, stop, increase, decrease, switch, continue - or unclear when the wording does not say.",
    )
    due_text: str | None = Field(
        default=None,
        description="Timing language exactly as written in the span (for example 'in three months'), or null if the plan states no timing.",
    )
    ambiguity_note: str | None = Field(
        default=None,
        description="Short note when the wording is unclear (for example the plan says 'labs' without naming a test); otherwise null.",
    )


class ModelExtractionOutput(StrictModel):
    """Structured output requested from the model."""

    commitments: list[ModelCommitment] = Field(default_factory=list, description="Empty when the plan states no explicit commitment.")
    warnings: list[str] = Field(default_factory=list, description="Short notes about ambiguity in the plan wording, if any.")


# --------------------------------------------------------------------------- #
# Follow-up turn (tool loop) contracts
# --------------------------------------------------------------------------- #


class ModelStatement(StrictModel):
    """One statement proposed by the model for a follow-up answer (untrusted until verified)."""

    text: str = Field(min_length=1, description="One plain sentence. Facts must come only from tool results; quote values exactly.")
    kind: StatementKind = Field(description="fact = supported by cited records; no_record_found = a search returned nothing; clarification = the question needs narrowing; refusal = the question asks for advice, another patient, or something outside the record.")
    citation_record_ids: list[str] = Field(default_factory=list, description="record_id values exactly as returned by tools in this turn. Required for fact; empty for no_record_found.")


class ModelTurnAnswer(StrictModel):
    """Structured answer requested from the model at the end of a turn."""

    statements: list[ModelStatement] = Field(default_factory=list)


class ToolCall(StrictModel):
    """A tool the model asked to run."""

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(StrictModel):
    """A tool output handed back to the model."""

    call_id: str
    content: str


class TurnStep(StrictModel):
    """One model step in the loop: either tool calls to run, or the final answer."""

    tool_calls: list[ToolCall] = Field(default_factory=list)
    answer: ModelTurnAnswer | None = None
    usage: ModelUsage
    assistant_content: Any = Field(default=None, description="Provider-specific assistant turn to append to the transcript before tool results.")


class ModelUsage(StrictModel):
    """Non-clinical metadata about one provider call."""

    provider: str
    model: str
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int = Field(ge=0)


class ModelExtractionResult(StrictModel):
    output: ModelExtractionOutput
    usage: ModelUsage


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ProviderError(Exception):
    """Base class. Messages are fixed strings: never keys, prompts, note text or raw responses."""

    retryable: bool = False


class ProviderConfigurationError(ProviderError):
    """Provider cannot be constructed or the request was rejected as invalid. Not retryable."""


class ProviderAuthenticationError(ProviderError):
    """Credentials rejected. Not retryable."""


class ProviderRejectedRequestError(ProviderError):
    """The provider answered 4xx to a well-formed call (schema/parameter problem). Not retryable; a code bug, not an outage."""


class ProviderTimeoutError(ProviderError):
    retryable = True


class ProviderRateLimitError(ProviderError):
    retryable = True


class ProviderUnavailableError(ProviderError):
    """Connection failure or provider-side 5xx/overload."""

    retryable = True


class MalformedModelOutputError(ProviderError):
    """The model did not return output matching the requested schema."""

    retryable = True


# --------------------------------------------------------------------------- #
# Port
# --------------------------------------------------------------------------- #


@runtime_checkable
class ModelProvider(Protocol):
    """Narrow, replaceable boundary: structured extraction and one tool-loop step."""

    name: str

    async def extract_commitments(self, plan_text: str) -> ModelExtractionResult:
        """Return the model's proposed commitments for ``plan_text`` or raise a ``ProviderError``."""
        ...

    async def turn_step(
        self,
        system: str,
        transcript: list[Any],
        tools: list[dict[str, Any]],
        *,
        force_answer: bool,
    ) -> TurnStep:
        """Run one model step over ``transcript`` (provider-shaped messages).

        Returns tool calls to execute, or the final ``ModelTurnAnswer``. With
        ``force_answer`` the model must answer now (no more tools). Raises a
        ``ProviderError`` on failure.
        """
        ...

    @staticmethod
    def tool_results_message(results: list[tuple[str, str]]) -> Any:
        """Provider-shaped message carrying (call_id, serialized output) pairs back to the model."""
        ...

    async def ping(self) -> bool:
        """Cheap reachability check for /ready (a models lookup, never a completion)."""
        ...


TurnStep.model_rebuild()


__all__ = [
    "MalformedModelOutputError",
    "ModelCommitment",
    "ModelExtractionOutput",
    "ModelExtractionResult",
    "ModelProvider",
    "ModelStatement",
    "ModelTurnAnswer",
    "ModelUsage",
    "ToolCall",
    "ToolResult",
    "TurnStep",
    "ProviderAuthenticationError",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderRejectedRequestError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
]
