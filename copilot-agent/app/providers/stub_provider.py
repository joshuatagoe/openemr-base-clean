"""StubProvider: a deterministic, no-network ModelProvider for load tests and demos without spend.

Extraction: proposes one ``lab_test`` commitment per sentence that names a
test from the curated table and one ``medication`` commitment per sentence
with a start/stop/continue verb followed by a word - all spans verbatim from
the note, so grounding passes exactly as it would for a careful model.
Turns: answers every question with a fixed ``refusal`` statement and never
calls a tool. Selected with ``MODEL_PROVIDER=stub``; ``/ready`` reports it
as degraded so it can never be mistaken for the real provider in production.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, ClassVar

from app.contracts import CommitmentKind, MedicationAction, StatementKind
from app.providers.base import (
    ContentPart,
    ModelCommitment,
    ModelExtractionOutput,
    ModelExtractionResult,
    ModelStatement,
    ModelTurnAnswer,
    ModelUsage,
    ParseResult,
    ProviderConfigurationError,
    SchemaT,
    TextPart,
    TurnStep,
)
from app.synonyms import resolve_commitment

_SENTENCE = re.compile(r"[^.;\n]+[.;]?")
_MED_VERB = re.compile(r"\b(start|stop|continue|increase|decrease|switch)\b\s+([A-Za-z][A-Za-z-]+)", re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]*")


class StubProvider:
    name = "stub"

    def __init__(self, model: str = "stub-deterministic") -> None:
        self.model = model

    # Schema -> deterministic builder. New document types register a fixture here
    # instead of adding a method to this class or to the ModelProvider port
    # (open/closed; see W2_ARCHITECTURE_DECISIONS.md "Design principles").
    _fixtures: ClassVar[dict[type, Callable[[list[ContentPart]], Any]]] = {}

    @classmethod
    def register_fixture(cls, schema: type, builder: Callable[[list[ContentPart]], Any]) -> None:
        """Teach the stub to produce a deterministic instance of ``schema``."""
        cls._fixtures[schema] = builder

    def _usage(self) -> ModelUsage:
        return ModelUsage(provider=self.name, model=self.model, input_tokens=0, output_tokens=0, latency_ms=0)

    async def parse_structured(
        self,
        *,
        system: str,
        content: list[ContentPart],
        schema: type[SchemaT],
        max_tokens: int,
        effort: str | None = None,
    ) -> ParseResult[SchemaT]:
        if schema is ModelExtractionOutput:
            text = "\n".join(p.text for p in content if isinstance(p, TextPart))
            return ParseResult(output=self._extract(text), usage=self._usage())  # type: ignore[arg-type]
        builder = self._fixtures.get(schema)
        if builder is None:
            raise ProviderConfigurationError("stub provider has no fixture registered for the requested schema")
        return ParseResult(output=builder(content), usage=self._usage())

    async def extract_commitments(self, plan_text: str) -> ModelExtractionResult:
        """Week 1 convenience wrapper. Deliberately **not** a ``ModelProvider`` member."""
        return ModelExtractionResult(output=self._extract(plan_text), usage=self._usage())

    def _extract(self, plan_text: str) -> ModelExtractionOutput:
        commitments: list[ModelCommitment] = []
        for m in _SENTENCE.finditer(plan_text):
            sentence = m.group(0).strip()
            if not sentence:
                continue
            med = _MED_VERB.search(sentence)
            if med:
                commitments.append(ModelCommitment(kind=CommitmentKind.MEDICATION, source_span=sentence, drug_name=med.group(2), action=MedicationAction(med.group(1).lower())))
                continue
            words = [w.group(0) for w in _WORD.finditer(sentence)]
            candidates = [" ".join(words[i:i + n]) for n in (3, 2, 1) for i in range(len(words) - n + 1)]
            for candidate in candidates:  # longest phrase first, e.g. "fasting lipid panel" before "lipid"
                if resolve_commitment(candidate) is not None:
                    commitments.append(ModelCommitment(kind=CommitmentKind.LAB_TEST, source_span=sentence, test_name=candidate))
                    break
        return ModelExtractionOutput(commitments=commitments)

    async def turn_step(self, system: str, transcript: list[Any], tools: list[dict[str, Any]], *, force_answer: bool) -> TurnStep:
        answer = ModelTurnAnswer(statements=[ModelStatement(text="The stub provider is active; follow-up answers are unavailable in this configuration.", kind=StatementKind.REFUSAL, citation_record_ids=[])])
        return TurnStep(answer=answer, usage=ModelUsage(provider=self.name, model=self.model, input_tokens=0, output_tokens=0, latency_ms=0), assistant_content=[{"type": "text", "text": "stub"}])

    async def ping(self) -> bool:
        return True

    @staticmethod
    def tool_results_message(results: list[tuple[str, str]]) -> dict[str, Any]:
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": content} for cid, content in results]}


__all__ = ["StubProvider"]
