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

from typing import Protocol, runtime_checkable

from pydantic import Field

from app.contracts import CommitmentKind, StrictModel

# --------------------------------------------------------------------------- #
# Model-facing (internal, untrusted until verified) contracts
# --------------------------------------------------------------------------- #


class ModelCommitment(StrictModel):
    """One explicit follow-up commitment as proposed by the model.

    Field descriptions double as the JSON-schema guidance the model sees.
    """

    kind: CommitmentKind = Field(description="lab_test for a test/lab to be obtained; medication for a start/stop/continue/change of a drug.")
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
    due_text: str | None = Field(
        default=None,
        description="Timing language exactly as written in the span (for example 'in three months'), or null if the plan states no timing.",
    )


class ModelExtractionOutput(StrictModel):
    """Structured output requested from the model."""

    commitments: list[ModelCommitment] = Field(default_factory=list, description="Empty when the plan states no explicit commitment.")
    warnings: list[str] = Field(default_factory=list, description="Short notes about ambiguity in the plan wording, if any.")


class ModelUsage(StrictModel):
    """Non-clinical metadata about one provider call."""

    provider: str
    model: str
    input_tokens: int | None = None
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
    """Narrow, replaceable boundary for structured commitment extraction."""

    name: str

    async def extract_commitments(self, plan_text: str) -> ModelExtractionResult:
        """Return the model's proposed commitments for ``plan_text`` or raise a ``ProviderError``."""
        ...


__all__ = [
    "MalformedModelOutputError",
    "ModelCommitment",
    "ModelExtractionOutput",
    "ModelExtractionResult",
    "ModelProvider",
    "ModelUsage",
    "ProviderAuthenticationError",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
]
