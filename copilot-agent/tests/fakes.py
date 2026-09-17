"""Test doubles shared across suites. Nothing here touches the network."""

from __future__ import annotations

from typing import Any

from app.contracts import CommitmentKind
from app.providers.base import ModelCommitment, ModelExtractionOutput, ModelExtractionResult, ModelUsage


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


class FakeProvider:
    """ModelProvider that replays scripted results/exceptions and records calls.

    The last script item repeats, so a single item scripts every call.
    """

    name = "fake"

    def __init__(self, *script: ModelExtractionOutput | Exception) -> None:
        self._script = list(script) or [model_output()]
        self.calls: list[str] = []

    async def extract_commitments(self, plan_text: str) -> ModelExtractionResult:
        self.calls.append(plan_text)
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, Exception):
            raise item
        return ModelExtractionResult(output=item, usage=fake_usage())
