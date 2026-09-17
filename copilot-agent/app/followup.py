"""Scoped follow-up turn (UC-04; ARCHITECTURE.md section 8, "Scoped follow-up flow").

One turn = a bounded tool loop over the stored bundle, then the verifier.
The model chooses tools (strict schemas) for at most ``MAX_TOOL_ITERATIONS``
steps; after that it must answer. Every statement passes ``verify_turn``
before it is returned; nothing the model wrote reaches the panel unverified.
Conversation history is the bound bundle's own verified turns and nothing
else - a turn can never see another patient.

Framework-independent: the provider port supplies one model step at a time;
this module owns the loop, the tool execution and the transcript.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.contracts import ContextBundle, EvidenceMatch, ToolCallRecord, VerifiedStatement
from app.providers.base import ModelProvider, ModelTurnAnswer
from app.providers.prompt import FOLLOWUP_SYSTEM_PROMPT, build_question_content
from app.tools import ToolOutput, run_tool, serialize_output, tool_definitions
from app.verifier import TurnEvidence, verify_turn

MAX_TOOL_ITERATIONS = 3
MAX_HISTORY_TURNS = 10


@dataclass
class ConversationTurn:
    """One completed turn kept for context: the question and the verified statements only."""

    question: str
    statements: list[VerifiedStatement]


@dataclass
class TurnOutcome:
    statements: list[VerifiedStatement]
    rejected_count: int
    rejection_codes: list[str]
    tool_calls: list[ToolCallRecord]
    iterations: int


def _history_messages(history: list[ConversationTurn]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in history[-MAX_HISTORY_TURNS:]:
        messages.append({"role": "user", "content": build_question_content(turn.question)})
        text = " ".join(s.text for s in turn.statements) or "No verified statements were returned for that question."
        messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    return messages


def _record(call_name: str, args: dict[str, Any], output: ToolOutput) -> ToolCallRecord:
    safe_args = {k: (v if isinstance(v, (str, int, bool)) or v is None else str(v)) for k, v in args.items()}
    return ToolCallRecord(tool=call_name, args=safe_args, records=len(output.records), truncated=output.truncated, error=output.error)


async def run_turn(
    provider: ModelProvider,
    bundle: ContextBundle,
    matches: list[EvidenceMatch],
    history: list[ConversationTurn],
    question: str,
    *,
    max_iterations: int = MAX_TOOL_ITERATIONS,
) -> TurnOutcome:
    """Run one turn. Raises ``ProviderError`` on model failure; tool failures are returned as errors, never raised."""
    transcript: list[Any] = [*_history_messages(history), {"role": "user", "content": build_question_content(question)}]
    tools = tool_definitions()
    outputs: list[ToolOutput] = []
    records: list[ToolCallRecord] = []
    answer: ModelTurnAnswer | None = None
    iterations = 0

    for step_index in range(max_iterations + 1):
        force = step_index >= max_iterations
        step = await provider.turn_step(FOLLOWUP_SYSTEM_PROMPT, transcript, tools, force_answer=force)
        if step.answer is not None:
            answer = step.answer
            break
        iterations += 1
        transcript.append({"role": "assistant", "content": step.assistant_content})
        results: list[tuple[str, str]] = []
        for call in step.tool_calls:
            output = run_tool(bundle, matches, call.name, call.arguments)
            outputs.append(output)
            records.append(_record(call.name, call.arguments, output))
            results.append((call.call_id, serialize_output(output)))
        transcript.append(provider.tool_results_message(results))

    if answer is None:  # provider returned tool calls even when forced: nothing verifiable to say
        answer = ModelTurnAnswer(statements=[])

    kept, rejected, codes = verify_turn(answer, TurnEvidence(outputs))
    return TurnOutcome(statements=kept, rejected_count=rejected, rejection_codes=codes, tool_calls=records, iterations=iterations)


__all__ = ["MAX_HISTORY_TURNS", "MAX_TOOL_ITERATIONS", "ConversationTurn", "TurnOutcome", "run_turn"]
