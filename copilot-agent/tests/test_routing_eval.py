"""Tool-routing evaluation tier (ARCHITECTURE.md section 15; EVAL.md "Tool routing").

The deterministic tests prove the rubric itself: a scripted provider makes
known routing decisions and the scorer must grade them correctly, so a
regression in the live number is a model or prompt change, not a scoring bug.
The live test samples the real model N times per case (opt-in, spends money).
"""

from __future__ import annotations

import os
import sys

import pytest

from app.routing_eval import (
    ROUTING_ACCURACY_TARGET,
    RoutingCase,
    load_suite,
    render_section,
    run_suite,
    score_sample,
)
from app.tools import TOOL_ARGS
from tests.fakes import FakeProvider, answer, calls, statement

REFUSAL = "This question is outside what the Co-Pilot can check."
SUITE = load_suite()
BY_NAME = {c.name: c for c in SUITE.cases}


# --------------------------------------------------------------------------- #
# Case file invariants
# --------------------------------------------------------------------------- #


def test_every_routing_case_is_labelled_and_names_real_tools() -> None:
    """Regression guard: each case is self-describing and can only name tools from the inventory."""
    assert len(SUITE.cases) >= 10
    assert str(SUITE.bundle.patient_uuid) == "3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e"
    assert len({c.name for c in SUITE.cases}) == len(SUITE.cases)
    for case in SUITE.cases:
        assert case.guards and case.question
        assert set(case.must_call) <= set(TOOL_ARGS), case.name
        assert set(case.may_call) <= set(TOOL_ARGS), case.name
        assert not (set(case.must_call) & set(case.may_call)), case.name
    assert any(c.out_of_scope for c in SUITE.cases) and any(not c.expect_refusal for c in SUITE.cases)


# --------------------------------------------------------------------------- #
# Rubric
# --------------------------------------------------------------------------- #


async def _outcome(provider: FakeProvider, case: RoutingCase):
    from app.routing_eval import briefing_matches
    from app.followup import run_turn

    return await run_turn(provider, SUITE.bundle, briefing_matches(SUITE.bundle), [], case.question)


@pytest.mark.anyio
async def test_rubric_passes_the_expected_route() -> None:
    case = BY_NAME["latest_result_uses_find_results"]
    provider = FakeProvider(turn_script=[
        calls(("find_results", {"test_query": "HbA1c"})),
        answer(statement("The latest HbA1c was 8.9 % on 2026-09-08.", "fact", "procedure_result:9001")),
    ])
    score = score_sample(case, await _outcome(provider, case))
    assert score.tools == ("find_results",) and score.kinds == ("fact",)
    assert score.routing_ok and score.outcome_ok and score.passed


@pytest.mark.anyio
async def test_rubric_fails_a_wrong_tool_even_when_the_answer_verifies() -> None:
    """Guards: a correct-looking answer reached through the wrong source is still a routing failure."""
    case = BY_NAME["latest_result_uses_find_results"]
    provider = FakeProvider(turn_script=[
        calls(("list_commitments", {})),
        answer(statement("HbA1c: matching result found.", "fact", "procedure_result:9001")),
    ])
    score = score_sample(case, await _outcome(provider, case))
    assert score.routing_ok is False and score.outcome_ok is True and not score.passed


@pytest.mark.anyio
async def test_rubric_requires_every_must_call_tool_and_tolerates_may_call() -> None:
    two = BY_NAME["two_sources_in_one_turn"]
    only_one = FakeProvider(turn_script=[calls(("find_results", {"test_query": "potassium"})), answer(statement("Potassium 4.1 mmol/L.", "fact", "procedure_result:9002"))])
    assert score_sample(two, await _outcome(only_one, two)).routing_ok is False

    order = BY_NAME["order_status_uses_find_orders"]
    with_extra = FakeProvider(turn_script=[
        calls(("find_orders", {"test_query": "lipid panel"}), ("find_results", {"test_query": "lipid panel"})),
        answer(statement("A lipid panel order is recorded as pending.", "fact", "procedure_order:5")),
    ])
    assert score_sample(order, await _outcome(with_extra, order)).passed


@pytest.mark.anyio
async def test_rubric_out_of_scope_fails_on_any_tool_call_or_missing_refusal() -> None:
    """Guards: the safety slice - an out-of-scope question must refuse without touching a tool."""
    case = BY_NAME["vitals_refused_without_tools"]
    clean = FakeProvider(turn_script=[answer(statement(REFUSAL, "refusal"))])
    assert score_sample(case, await _outcome(clean, case)).passed

    leaked = FakeProvider(turn_script=[calls(("find_results", {"test_query": "blood pressure"})), answer(statement(REFUSAL, "refusal"))])
    score = score_sample(case, await _outcome(leaked, case))
    assert score.routing_ok is False and score.outcome_ok is True

    answered = FakeProvider(turn_script=[answer(statement("No record found for blood pressure.", "no_record_found"))])
    score = score_sample(case, await _outcome(answered, case))
    assert score.routing_ok is True and score.outcome_ok is False


@pytest.mark.anyio
async def test_rubric_in_scope_fails_when_the_model_refuses_instead() -> None:
    case = BY_NAME["medication_uses_find_medications"]
    provider = FakeProvider(turn_script=[answer(statement(REFUSAL, "refusal"))])
    score = score_sample(case, await _outcome(provider, case))
    assert score.routing_ok is False and score.outcome_ok is False


# --------------------------------------------------------------------------- #
# Runner and report
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_run_suite_reports_accuracy_and_leak_rate_from_independent_samples() -> None:
    """Each sample gets a fresh provider and empty history; the report aggregates per case and overall."""
    scripts = iter(range(10_000))

    def factory() -> FakeProvider:
        n = next(scripts)
        if n % 2 == 0:  # alternate: a leaking refusal and a clean refusal / a right and a wrong route
            return FakeProvider(turn_script=[calls(("find_results", {"test_query": "x"})), answer(statement(REFUSAL, "refusal"))])
        return FakeProvider(turn_script=[answer(statement(REFUSAL, "refusal"))])

    two_cases = SUITE.model_copy(update={"cases": [BY_NAME["vitals_refused_without_tools"], BY_NAME["other_notes_refused_without_tools"]]})
    report = await run_suite(factory, two_cases, runs=2, model="fake")
    assert report.samples == 4 and report.out_of_scope_samples == 4
    assert report.passed == 2 and report.accuracy == 0.5
    assert report.leaked == 2 and report.leak_rate == 0.5
    text = "\n".join(render_section(report))
    assert "routing accuracy **0.50** (2/4 samples)" in text and "leak rate **0.50**" in text
    assert "find_results ×1" in text and "none ×1" in text
    assert "3b9d2c1e" not in text and "8.9" not in text  # the report carries decisions, not record content


def test_render_section_without_a_run_says_so() -> None:
    text = "\n".join(render_section(None))
    assert "Not run for this revision" in text and "--routing 3" in text


# --------------------------------------------------------------------------- #
# Live tier (opt-in; N real turns per case)
# --------------------------------------------------------------------------- #


@pytest.mark.live
@pytest.mark.anyio
@pytest.mark.skipif(
    os.environ.get("RUN_ANTHROPIC_INTEGRATION_TEST") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="live Anthropic calls; set RUN_ANTHROPIC_INTEGRATION_TEST=1 and ANTHROPIC_API_KEY to run (ROUTING_EVAL_RUNS sets samples per case)",
)
async def test_live_routing_accuracy(capsys: pytest.CaptureFixture[str]) -> None:
    """Real model: routing accuracy over N samples per labelled question meets the KEY_METRICS.md §4 target,
    and no out-of-scope sample calls a tool. Prints the EVAL.md section so the run is recordable."""
    from app.providers.anthropic_provider import AnthropicProvider
    from app.settings import ModelSettings

    runs = int(os.environ.get("ROUTING_EVAL_RUNS", "3"))
    settings = ModelSettings()
    provider = AnthropicProvider(settings)
    report = await run_suite(lambda: provider, SUITE, runs=runs, model=settings.model_id_turn)
    with capsys.disabled():
        print("\n" + "\n".join(render_section(report)))
    failures = [(c.case.name, s.tools, s.kinds) for c in report.cases for s in c.samples if not s.passed]
    assert report.accuracy >= ROUTING_ACCURACY_TARGET, failures
    assert report.leaked == 0, failures
