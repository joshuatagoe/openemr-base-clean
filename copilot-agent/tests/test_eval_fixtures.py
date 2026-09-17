"""Fixture evaluation tier (ARCHITECTURE.md section 15): every labelled case must pass with no model,
and the aggregate metrics must meet the targets. The opt-in live test replaces the scripted model
output with real extraction and reports precision/recall.
"""

from __future__ import annotations

import json
import os

import pytest

from app.eval import CASES_DIR, EvalCase, load_cases, run_case, score_extraction, summarize
from app.extractor import CommitmentExtractor

CASES = load_cases()


def test_case_directory_is_populated() -> None:
    assert len(CASES) >= 15, "expected a first fixture set of at least 15 labelled cases"
    assert {c.test_class for c in CASES} >= {"boundary", "invariant", "adversarial", "missing_conflicting", "regression"}


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_fixture_case(case: EvalCase) -> None:
    result = run_case(case)
    assert result.passed, "\n".join(result.failures)


def test_aggregate_metrics_meet_targets() -> None:
    results = [run_case(c) for c in CASES]
    summary = summarize(CASES, results)
    assert summary.passed == summary.cases
    assert summary.hallucinated_spans == 0
    assert summary.state_precision >= 0.90
    assert summary.citation_completeness == 1.0


def test_every_case_carries_its_labels() -> None:
    """Regression guard: cases are self-describing (class + guarded failure mode) and reproducible from the generator."""
    for path in sorted(CASES_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["test_class"] and raw["guards"], path.name
        assert raw["bundle"]["patient_uuid"] == "3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e"
        assert "Evelyn" not in path.read_text(encoding="utf-8")


@pytest.mark.live
@pytest.mark.anyio
@pytest.mark.skipif(os.environ.get("RUN_ANTHROPIC_INTEGRATION_TEST") != "1", reason="set RUN_ANTHROPIC_INTEGRATION_TEST=1 to run live extraction over the fixture cases")
async def test_live_extraction_over_fixture_cases(capsys: pytest.CaptureFixture[str]) -> None:
    from app.providers.anthropic_provider import AnthropicProvider

    extractor = CommitmentExtractor(AnthropicProvider())
    # One provider call per distinct plan text; cases sharing a plan share the extraction.
    by_plan: dict[str, list[EvalCase]] = {}
    for case in CASES:
        if case.live:
            by_plan.setdefault(case.bundle.prior_note.plan_text, []).append(case)
    scores = []
    for plan_text, cases in by_plan.items():
        grounded = await extractor.extract(plan_text)
        for case in cases:
            scores.append((case.name, score_extraction(case, grounded), run_case(case, extraction=grounded)))
    with capsys.disabled():
        for name, score, result in scores:
            print(f"{name}: P={score['precision']:.2f} R={score['recall']:.2f} missed={score['missed']} extra={score['extra']} states_ok={result.passed}")
    tp = sum(s["true_positives"] for _, s, _ in scores)
    expected = sum(s["expected"] for _, s, _ in scores)
    extracted = sum(s["extracted"] for _, s, _ in scores)
    precision = tp / extracted if extracted else 1.0
    recall = tp / expected if expected else 1.0
    with capsys.disabled():
        print(f"overall extraction precision={precision:.2f} recall={recall:.2f}")
    assert precision >= 0.90 and recall >= 0.85
