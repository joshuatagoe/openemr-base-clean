"""Commitment extraction pipeline.

    PriorNote.plan_text
        -> ModelProvider (minimum-necessary text only)
        -> ModelExtractionOutput (schema-validated, still untrusted)
        -> deterministic grounding: exact source-span verification,
           per-kind field checks, de-duplication, source ordering, result-scoped ids
        -> ExtractionOutput (public contract)

The model proposes; this module verifies. Nothing the model says about a
commitment reaches the matcher unless its ``source_span`` is an exact,
non-empty substring of the plan text (ARCHITECTURE.md section 9,
"Unsupported-claim rejection"). Rejections produce generic, non-clinical
warnings; the rejected text is never echoed.

Model-authored free-text ``warnings`` are not forwarded: they are unverified
prose. Their count is reported instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.contracts import CommitmentKind, ExtractedCommitment, ExtractionOutput, MedicationAction
from app.providers.base import (
    ModelCommitment,
    ModelExtractionOutput,
    ModelProvider,
    ModelUsage,
    ProviderError,
)


def record_usage(usage: ModelUsage) -> None:
    """Token and estimated-cost accounting for one model call (PHI-free)."""
    from app.metrics import metrics
    from app.observability import log_event
    from app.settings import ModelSettings

    cost = metrics.add_usage(usage.model, usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, ModelSettings().price_table())
    log_event("model.usage", provider=usage.provider, model=usage.model, input_tokens=usage.input_tokens, cached_input_tokens=usage.cached_input_tokens, output_tokens=usage.output_tokens, latency_ms=usage.latency_ms, estimated_cost_usd=round(cost, 6))

REJECTED_SPAN_WARNING = (
    "A model-generated commitment was rejected because its source span was not grounded in the supplied plan."
)
REJECTED_LAB_NAME_WARNING = (
    "A model-generated lab commitment was rejected because its test name was not found in its source span."
)
REJECTED_DRUG_NAME_WARNING = (
    "A model-generated medication commitment was rejected because its drug name was not found in its source span."
)
DROPPED_DUE_TEXT_WARNING = (
    "Timing text on a model-generated commitment was dropped because it was not found in its source span."
)
DUPLICATE_WARNING = "A duplicate model-generated commitment was collapsed."
MODEL_NOTES_WARNING = "The model reported {n} unverified note(s); they were not forwarded."


def stable_commitment_id(position: int) -> str:
    """Result-scoped id (``c-001``, ``c-002``, ...) from the commitment's 1-based position.

    Derived from nothing but ordering after grounding, de-duplication and
    source-order sorting, so it carries no clinical text and is identical for
    identical extraction results. Unique within one ``ExtractionOutput``;
    later orchestration may namespace it.
    """
    if position < 1:
        raise ValueError("position is 1-based")
    return f"c-{position:03d}"


@dataclass(frozen=True)
class _Grounded:
    position: int
    original_index: int
    commitment: ModelCommitment


def _contains_ci(haystack: str, needle: str) -> bool:
    return needle.strip() != "" and needle.strip().lower() in haystack.lower()


MAX_NOTE_CHARS = 160


def _bounded_note(note: str | None) -> str | None:
    """Keep the model's ambiguity note short and plain; it is rendered as text only."""
    if note is None:
        return None
    cleaned = " ".join(note.split())
    if not cleaned:
        return None
    return cleaned[:MAX_NOTE_CHARS]


def ground_extraction(plan_text: str, output: ModelExtractionOutput) -> ExtractionOutput:
    """Deterministically verify and normalize model output against ``plan_text``.

    Pure. Neither argument is mutated.
    """
    warnings: list[str] = []
    grounded: list[_Grounded] = []
    seen: set[tuple[str, str, str | None, str | None, str | None]] = set()

    for index, proposed in enumerate(output.commitments):
        span = proposed.source_span
        position = plan_text.find(span) if span else -1
        if position < 0:
            warnings.append(REJECTED_SPAN_WARNING)
            continue

        commitment = proposed
        if commitment.kind is CommitmentKind.LAB_TEST:
            # A lab commitment may leave the test unnamed ("check labs"); a named test must appear in the span.
            if commitment.test_name is not None and not _contains_ci(span, commitment.test_name):
                warnings.append(REJECTED_LAB_NAME_WARNING)
                continue
        elif commitment.kind is CommitmentKind.MEDICATION:
            if commitment.drug_name is not None and not _contains_ci(span, commitment.drug_name):
                warnings.append(REJECTED_DRUG_NAME_WARNING)
                continue
            if commitment.action is None:
                commitment = commitment.model_copy(update={"action": MedicationAction.UNCLEAR})
        else:
            # kind 'other' carries no names; drop any the model attached.
            if commitment.test_name is not None or commitment.drug_name is not None:
                commitment = commitment.model_copy(update={"test_name": None, "drug_name": None})

        if commitment.due_text is not None and commitment.due_text not in span:
            warnings.append(DROPPED_DUE_TEXT_WARNING)
            commitment = commitment.model_copy(update={"due_text": None})

        key = (commitment.kind.value, span, commitment.test_name, commitment.drug_name, commitment.action, commitment.due_text)  # ambiguity_note is free text; not part of identity
        if key in seen:
            warnings.append(DUPLICATE_WARNING)
            continue
        seen.add(key)
        grounded.append(_Grounded(position=position, original_index=index, commitment=commitment))

    grounded.sort(key=lambda g: (g.position, g.original_index))

    commitments: list[ExtractedCommitment] = []
    for position, item in enumerate(grounded, start=1):
        c = item.commitment
        commitments.append(
            ExtractedCommitment(
                commitment_id=stable_commitment_id(position),
                kind=c.kind,
                source_span=c.source_span,
                test_name=c.test_name,
                drug_name=c.drug_name,
                action=c.action if c.kind is CommitmentKind.MEDICATION else None,
                due_text=c.due_text,
                ambiguity_note=_bounded_note(c.ambiguity_note),
            )
        )

    if output.warnings:
        warnings.append(MODEL_NOTES_WARNING.format(n=len(output.warnings)))

    return ExtractionOutput(commitments=commitments, warnings=warnings)


class CommitmentExtractor:
    """Orchestrates one extraction: bounded provider attempts, then grounding."""

    def __init__(self, provider: ModelProvider, *, max_attempts: int = 2) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._provider = provider
        self._max_attempts = max_attempts

    @property
    def provider_name(self) -> str:
        return self._provider.name

    async def extract(self, plan_text: str) -> ExtractionOutput:
        """Extract grounded commitments from ``plan_text``.

        Raises ``ProviderError`` (never returns an empty result) when the
        provider fails after the bounded attempts, so a model outage is not
        mistaken for "no commitments".
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                result = await self._provider.extract_commitments(plan_text)
            except ProviderError as exc:
                if exc.retryable and attempt < self._max_attempts:
                    continue
                raise
            record_usage(result.usage)
            return ground_extraction(plan_text, result.output)


__all__ = [
    "CommitmentExtractor",
    "DROPPED_DUE_TEXT_WARNING",
    "DUPLICATE_WARNING",
    "MODEL_NOTES_WARNING",
    "REJECTED_DRUG_NAME_WARNING",
    "REJECTED_LAB_NAME_WARNING",
    "REJECTED_SPAN_WARNING",
    "ground_extraction",
    "stable_commitment_id",
]
