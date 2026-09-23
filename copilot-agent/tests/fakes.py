"""Test doubles shared across suites. Nothing here touches the network."""

from __future__ import annotations

from typing import Any

from app.contracts import CommitmentKind
from app.providers.base import (
    ContentPart,
    ParseResult,
    TextPart,
    ModelCommitment,
    ModelExtractionOutput,
    ModelExtractionResult,
    ModelStatement,
    ModelTurnAnswer,
    ModelUsage,
    ToolCall,
    TurnStep,
)


def fake_usage() -> ModelUsage:
    return ModelUsage(provider="fake", model="fake-model", input_tokens=10, output_tokens=5, latency_ms=1)


def model_output(*commitments: ModelCommitment, warnings: list[str] | None = None) -> ModelExtractionOutput:
    return ModelExtractionOutput(commitments=list(commitments), warnings=warnings or [])


def hba1c(**overrides: Any) -> ModelCommitment:
    base: dict[str, Any] = {
        "kind": CommitmentKind.LAB_TEST,
        "source_span": "Repeat HbA1c in three months.",
        "test_name": "HbA1c",
        "due_text": "in three months",
    }
    base.update(overrides)
    return ModelCommitment(**base)


def metformin(**overrides: Any) -> ModelCommitment:
    base: dict[str, Any] = {"kind": CommitmentKind.MEDICATION, "source_span": "Continue metformin.", "drug_name": "metformin"}
    base.update(overrides)
    return ModelCommitment(**base)


def statement(text: str, kind: str = "fact", *cited: str) -> ModelStatement:
    return ModelStatement(text=text, kind=kind, citation_record_ids=list(cited))  # type: ignore[arg-type]


def answer(*statements: ModelStatement) -> ModelTurnAnswer:
    return ModelTurnAnswer(statements=list(statements))


def calls(*pairs: tuple[str, dict[str, Any]]) -> list[ToolCall]:
    return [ToolCall(call_id=f"call-{i}", name=name, arguments=args) for i, (name, args) in enumerate(pairs, start=1)]


class FakeProvider:
    """ModelProvider that replays scripted results/exceptions and records calls.

    The last extraction script item repeats, so a single item scripts every
    extraction. ``turn_script`` items are consumed one per ``turn_step``: a
    list of ToolCall (the model asks for tools), a ModelTurnAnswer (the model
    answers), or an Exception. When the script runs out the fake answers with
    no statements. ``force_answer`` with a tool-call item yields an empty answer.
    """

    name = "fake"

    def __init__(self, *script: ModelExtractionOutput | Exception, turn_script: list[Any] | None = None) -> None:
        self._script = list(script) or [model_output()]
        self.calls: list[str] = []
        self._turn_script = list(turn_script or [])
        self.turn_transcripts: list[list[Any]] = []
        self.forced: list[bool] = []

    async def turn_step(self, system: str, transcript: list[Any], tools: list[dict[str, Any]], *, force_answer: bool) -> TurnStep:
        self.turn_transcripts.append([dict(m) if isinstance(m, dict) else m for m in transcript])
        self.forced.append(force_answer)
        item = self._turn_script.pop(0) if self._turn_script else answer()
        if isinstance(item, Exception):
            raise item
        if isinstance(item, ModelTurnAnswer):
            return TurnStep(answer=item, usage=fake_usage(), assistant_content=[{"type": "text", "text": "answer"}])
        if force_answer:
            return TurnStep(answer=answer(), usage=fake_usage(), assistant_content=[{"type": "text", "text": "forced"}])
        return TurnStep(tool_calls=list(item), usage=fake_usage(), assistant_content=[{"type": "tool_use", "id": c.call_id, "name": c.name, "input": c.arguments} for c in item])

    @staticmethod
    def tool_results_message(results: list[tuple[str, str]]) -> dict[str, Any]:
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": content} for cid, content in results]}

    async def ping(self) -> bool:
        return True

    async def extract_commitments(self, plan_text: str) -> ModelExtractionResult:
        result = await self.parse_structured(
            system="", content=[TextPart(text=plan_text)], schema=ModelExtractionOutput, max_tokens=1
        )
        return ModelExtractionResult(output=result.output, usage=result.usage)

    async def parse_structured(
        self,
        *,
        system: str,
        content: list[ContentPart],
        schema: type[Any],
        max_tokens: int,
        effort: str | None = None,
    ) -> ParseResult[Any]:
        self.calls.append("\n".join(p.text for p in content if isinstance(p, TextPart)))
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, Exception):
            raise item
        return ParseResult(output=item, usage=fake_usage())
