"""Briefing orchestration: bundle -> plan text -> extractor -> matcher -> response.

Responsibilities stay separate (ARCHITECTURE.md sections 3 and 8):

* the model (behind ``CommitmentExtractor``) proposes commitments only;
* grounding inside the extractor verifies source spans;
* ``match_evidence`` assigns evidence states deterministically;
* this module selects the plan text, sequences the steps and assembles the
  ``BriefingResponse``. It never interprets results or recommends anything.

Provider failures propagate as ``ProviderError`` so the endpoint can return
an explicit error; an outage is never rendered as "no commitments".
Model-authored free text is never forwarded: the extractor already withholds
it, and this module adds only its own fixed warnings.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from app.contracts import BriefingRequest, BriefingResponse, ContextBundle
from app.extractor import CommitmentExtractor
from app.matcher import match_evidence
from app.providers.base import ModelProvider

NO_USABLE_PLAN_WARNING = (
    "No usable prior plan text was found in the supplied note; no commitments were evaluated."
)
PLAN_TOO_LONG_WARNING = (
    "The prior plan text exceeds the supported length; no commitments were evaluated."
)

DEFAULT_MAX_PLAN_CHARS = 20_000

_HAS_WORD = re.compile(r"[A-Za-z0-9]")


def select_plan_text(context: ContextBundle, *, max_chars: int = DEFAULT_MAX_PLAN_CHARS) -> tuple[str | None, list[str]]:
    """Deterministically choose the plan text to evaluate.

    The bundle carries one prior note (the module selects the baseline
    encounter). The text is usable when it contains at least one letter or
    digit and is within the supported length; otherwise ``None`` is returned
    with a fixed warning and no model call is made.
    """
    text = context.prior_note.plan_text
    if not _HAS_WORD.search(text):
        return None, [NO_USABLE_PLAN_WARNING]
    if len(text) > max_chars:
        return None, [PLAN_TOO_LONG_WARNING]
    return text, []


ProviderFactory = Callable[[], ModelProvider]


class BriefingService:
    """Builds one briefing per request.

    ``provider_factory`` is invoked lazily per briefing so that constructing
    the service (and importing the app) never requires provider credentials.
    """

    def __init__(self, provider_factory: ProviderFactory, *, max_plan_chars: int = DEFAULT_MAX_PLAN_CHARS) -> None:
        self._provider_factory = provider_factory
        self._max_plan_chars = max_plan_chars

    async def build_briefing(self, request: BriefingRequest) -> BriefingResponse:
        context = request.context
        plan_text, warnings = select_plan_text(context, max_chars=self._max_plan_chars)
        if plan_text is None:
            return BriefingResponse(
                correlation_id=context.correlation_id,
                patient_uuid=context.patient_uuid,
                matches=[],
                warnings=warnings,
            )

        extractor = CommitmentExtractor(self._provider_factory())
        extraction = await extractor.extract(plan_text)  # raises ProviderError on failure
        matches = match_evidence(context, extraction)

        return BriefingResponse(
            correlation_id=context.correlation_id,
            patient_uuid=context.patient_uuid,
            matches=matches,
            warnings=[*warnings, *extraction.warnings],
        )


__all__ = [
    "DEFAULT_MAX_PLAN_CHARS",
    "NO_USABLE_PLAN_WARNING",
    "PLAN_TOO_LONG_WARNING",
    "BriefingService",
    "ProviderFactory",
    "select_plan_text",
]
