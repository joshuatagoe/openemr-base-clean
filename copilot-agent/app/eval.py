"""Fixture evaluation tier (ARCHITECTURE.md section 15).

A case is one labelled ``ContextBundle`` plus a scripted model output and the
expected verified outcome. Running a case exercises grounding and matching
exactly as the service does, with no model call, so the tier is deterministic
and CI-runnable. The same cases feed the opt-in live extraction evaluation,
which replaces the scripted output with a real provider call and scores the
extracted spans against the labels.

Metrics (automated, section 15): evidence-state precision, citation
completeness, hallucinated-span rate; for live runs, extraction precision and
recall against labelled commitments.
"""

from __future__ import annotations

import sys

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import Field

from app.contracts import CommitmentKind, ContextBundle, EvidenceMatch, EvidenceState, ExtractionOutput, StrictModel
from app.extractor import ground_extraction
from app.matcher import match_evidence
from app.providers.base import ModelExtractionOutput

CASES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "cases"


class ExpectedCommitment(StrictModel):
    kind: CommitmentKind
    source_span: str
    state: EvidenceState | None = None
    cited: list[str] = Field(default_factory=list, description="Record ids that must be cited (subset check).")
    not_cited: list[str] = Field(default_factory=list, description="Record ids that must not be cited.")


class EvalCase(StrictModel):
    name: str
    live: bool = Field(default=True, description="False for cases whose labels describe scripted model misbehaviour; skipped by the live run.")
    test_class: str = Field(description="boundary | invariant | patient_isolation | adversarial | missing_conflicting | regression")
    guards: str = Field(description="The failure mode this case guards against.")
    bundle: ContextBundle
    model_output: ModelExtractionOutput = Field(description="Scripted model output (what the extractor would propose).")
    expected: list[ExpectedCommitment]
    expected_warnings: list[str] = Field(default_factory=list, description="Fixed warning strings that must be present.")


@dataclass
class CaseResult:
    name: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    matches: list[EvidenceMatch] = field(default_factory=list)
    extraction: ExtractionOutput | None = None


@dataclass
class EvalSummary:
    cases: int
    passed: int
    expected_commitments: int
    state_correct: int
    citations_required: int
    citations_present: int
    hallucinated_spans: int

    @property
    def state_precision(self) -> float:
        return self.state_correct / self.expected_commitments if self.expected_commitments else 1.0

    @property
    def citation_completeness(self) -> float:
        return self.citations_present / self.citations_required if self.citations_required else 1.0


def load_cases(directory: Path = CASES_DIR) -> list[EvalCase]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            cases.append(EvalCase.model_validate(json.load(fh)))
    return cases


def run_case(case: EvalCase, extraction: ExtractionOutput | None = None) -> CaseResult:
    """Ground the (scripted or supplied) extraction and match it; compare with the labels."""
    grounded = extraction if extraction is not None else ground_extraction(case.bundle.prior_note.plan_text, case.model_output)
    matches = match_evidence(case.bundle, grounded)
    result = CaseResult(name=case.name, passed=True, matches=matches, extraction=grounded)

    # Invariant: every grounded span is verbatim in the note (hallucination guard).
    for c in grounded.commitments:
        if c.source_span not in case.bundle.prior_note.plan_text:
            result.failures.append(f"hallucinated span: {c.source_span!r}")

    by_span = {(m.commitment.kind, m.commitment.source_span): m for m in matches}
    if len(by_span) != len(matches):
        result.failures.append("duplicate (kind, span) pairs in matches")
    for exp in case.expected:
        m = by_span.get((exp.kind, exp.source_span))
        if m is None:
            result.failures.append(f"missing commitment {exp.kind.value} {exp.source_span!r}")
            continue
        if m.state != exp.state:
            result.failures.append(f"{exp.source_span!r}: state {m.state} != expected {exp.state}")
        cited = {c.record_id for c in m.citations}
        for rid in exp.cited:
            if rid not in cited:
                result.failures.append(f"{exp.source_span!r}: missing citation {rid}")
        for rid in exp.not_cited:
            if rid in cited:
                result.failures.append(f"{exp.source_span!r}: must not cite {rid}")
    expected_pairs = {(e.kind, e.source_span) for e in case.expected}
    for key in by_span:
        if key not in expected_pairs:
            result.failures.append(f"unexpected commitment {key[0].value} {key[1]!r}")
    if extraction is None:  # warnings describe the scripted model output; not meaningful for a live extraction
        for w in case.expected_warnings:
            if w not in grounded.warnings:
                result.failures.append(f"missing warning: {w}")
    result.passed = not result.failures
    return result


def summarize(cases: list[EvalCase], results: list[CaseResult]) -> EvalSummary:
    expected_total = state_ok = cites_req = cites_ok = halluc = 0
    for case, res in zip(cases, results, strict=True):
        by_span = {(m.commitment.kind, m.commitment.source_span): m for m in res.matches}
        for exp in case.expected:
            expected_total += 1
            m = by_span.get((exp.kind, exp.source_span))
            if m is not None and m.state == exp.state:
                state_ok += 1
            cites_req += len(exp.cited)
            if m is not None:
                cited = {c.record_id for c in m.citations}
                cites_ok += sum(1 for rid in exp.cited if rid in cited)
        halluc += sum(1 for f in res.failures if f.startswith("hallucinated span"))
    return EvalSummary(
        cases=len(cases),
        passed=sum(1 for r in results if r.passed),
        expected_commitments=expected_total,
        state_correct=state_ok,
        citations_required=cites_req,
        citations_present=cites_ok,
        hallucinated_spans=halluc,
    )


def score_extraction(case: EvalCase, grounded: ExtractionOutput) -> dict[str, Any]:
    """Precision/recall of extracted (kind, span) pairs against the labels, for live runs."""
    expected = {(e.kind, e.source_span) for e in case.expected}
    got = {(c.kind, c.source_span) for c in grounded.commitments}
    tp = len(expected & got)
    return {
        "expected": len(expected),
        "extracted": len(got),
        "true_positives": tp,
        "precision": tp / len(got) if got else 1.0,
        "recall": tp / len(expected) if expected else 1.0,
        "missed": sorted(f"{k.value}:{s}" for k, s in expected - got),
        "extra": sorted(f"{k.value}:{s}" for k, s in got - expected),
    }


__all__ = ["CASES_DIR", "CaseResult", "EvalCase", "EvalSummary", "ExpectedCommitment", "load_cases", "run_case", "score_extraction", "summarize"]


# --------------------------------------------------------------------------- #
# Report (EVAL.md): every case with the failure mode it guards, and results
# --------------------------------------------------------------------------- #


async def _live_scores(cases: list[EvalCase]) -> dict[str, dict[str, Any]]:
    """One provider call per distinct plan text; cases sharing a plan share the extraction."""
    from app.extractor import CommitmentExtractor
    from app.providers.anthropic_provider import AnthropicProvider

    extractor = CommitmentExtractor(AnthropicProvider())
    by_plan: dict[str, list[EvalCase]] = {}
    for case in cases:
        if case.live:
            by_plan.setdefault(case.bundle.prior_note.plan_text, []).append(case)
    scores: dict[str, dict[str, Any]] = {}
    for plan_text, group in by_plan.items():
        grounded = await extractor.extract(plan_text)
        for case in group:
            scores[case.name] = {**score_extraction(case, grounded), "states_ok": run_case(case, extraction=grounded).passed}
    return scores


def render_report(cases: list[EvalCase], results: list[CaseResult], live: dict[str, dict[str, Any]] | None, *, model: str | None, when: str) -> str:
    summary = summarize(cases, results)
    lines = [
        "# Evaluation dataset and results — Clinical Co-Pilot",
        "",
        f"Generated by `uv run python -m app.eval --report` on {when}. Cases live in `copilot-agent/fixtures/cases/`",
        "(one JSON file each: a synthetic `ContextBundle`, the scripted model proposal, the labelled expectation, the",
        "test class and the failure mode the case guards). `tests/test_eval_fixtures.py` runs every case on each",
        "`uv run pytest`; the live tier is opt-in because it spends money.",
        "",
        "## Design",
        "",
        "- **Two tiers.** The *deterministic tier* replays a scripted model proposal through grounding, matching and",
        "  verification, so every case is reproducible with no model and no OpenEMR; it is the regression gate. The",
        "  *live tier* sends each distinct plan text to the configured model and scores the extraction against the",
        "  labelled commitments (precision/recall on `(kind, source_span)` pairs) and then re-runs the deterministic",
        "  checks on the live extraction.",
        "- **No happy-path-only cases.** Every case is a boundary condition, an invariant, or a regression risk, and",
        "  says which (`test_class`) and what it guards (`guards`), per the brief's engineering requirement. Cases",
        "  whose labels describe scripted model *misbehaviour* (a hallucinated span, an injected instruction) carry",
        "  `live: false`: they test the verifier, and a well-behaved model would not reproduce the input.",
        "- **Synthetic, self-authored data** on one fixed synthetic patient uuid; no real records, no names",
        "  (`test_every_case_carries_its_labels` enforces both). This measures the pipeline's behaviour on labelled",
        "  inputs, not clinical accuracy on real notes - KEY_METRICS.md §4 and §11 keep that distinction.",
        "",
        "## Results",
        "",
        "| Tier | Cases | Passed | Hallucinated spans | Evidence-state precision | Citation completeness |",
        "|---|---|---|---|---|---|",
        f"| Deterministic (scripted proposal → grounding → matching → verification) | {summary.cases} | {summary.passed} | {summary.hallucinated_spans} | {summary.state_precision:.2f} | {summary.citation_completeness:.2f} |",
    ]
    if live:
        tp = sum(s["true_positives"] for s in live.values())
        expected = sum(s["expected"] for s in live.values())
        extracted = sum(s["extracted"] for s in live.values())
        precision = tp / extracted if extracted else 1.0
        recall = tp / expected if expected else 1.0
        states_ok = sum(1 for s in live.values() if s["states_ok"])
        lines.append(f"| Live extraction (`{model}`) → same deterministic checks | {len(live)} | {states_ok} | — | extraction precision {precision:.2f} / recall {recall:.2f} | — |")
    lines += [
        "",
        "Targets (KEY_METRICS.md §4): hallucinated spans 0; evidence-state precision ≥ 0.90; citation completeness 1.00;",
        "live extraction precision ≥ 0.90 and recall ≥ 0.85. The unit and integration suite (`uv run pytest`) adds the",
        "patient-isolation, ticket, contract, verifier, tracing, resilience and API-collection tests around these cases.",
        "",
        "## Cases",
        "",
        "| # | Case | Class | Guards against | Deterministic | Live |",
        "|---|---|---|---|---|---|",
    ]
    by_name = {r.name: r for r in results}
    for i, case in enumerate(cases, 1):
        r = by_name[case.name]
        det = "pass" if r.passed else "FAIL: " + "; ".join(r.failures)[:120]
        if not case.live:
            lv = "n/a (scripted misbehaviour)"
        elif live and case.name in live:
            s = live[case.name]
            lv = f"P {s['precision']:.2f} / R {s['recall']:.2f}" + ("" if s["states_ok"] else " states FAIL") + (f"; extra {s['extra']}" if s["extra"] else "") + (f"; missed {s['missed']}" if s["missed"] else "")
        else:
            lv = "not run"
        lines.append(f"| {i} | `{case.name}` | {case.test_class} | {case.guards} | {det} | {lv} |")
    lines += ["", "## Reproduce", "", "```sh", "uv run pytest tests/test_eval_fixtures.py -q                       # deterministic tier",
              "RUN_ANTHROPIC_INTEGRATION_TEST=1 uv run pytest -k live_extraction   # live tier (one call per distinct plan)",
              "uv run python -m app.eval --report --live --out ../EVAL.md         # this file", "```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import asyncio
    from datetime import UTC, datetime

    ap = argparse.ArgumentParser(description="Fixture-tier evaluation; --report renders EVAL.md to stdout")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--live", action="store_true", help="also run the live extraction tier (spends money)")
    ap.add_argument("--out", type=Path, default=None, help="write the report here (UTF-8) instead of stdout")
    args = ap.parse_args(argv)
    cases = load_cases()
    results = [run_case(c) for c in cases]
    live = None
    model = None
    if args.live:
        from app.settings import ModelSettings

        model = ModelSettings().model_id_extraction
        live = asyncio.run(_live_scores(cases))
    if args.report:
        report = render_report(cases, results, live, model=model, when=datetime.now(UTC).strftime("%Y-%m-%d"))
        if args.out is not None:
            args.out.write_text(report, encoding="utf-8", newline="\n")
        else:
            sys.stdout.buffer.write(report.encode("utf-8"))
        return 0
    summary = summarize(cases, results)
    print(f"cases={summary.cases} passed={summary.passed} hallucinated_spans={summary.hallucinated_spans} state_precision={summary.state_precision:.2f} citation_completeness={summary.citation_completeness:.2f}")
    return 0 if summary.passed == summary.cases else 1


if __name__ == "__main__":
    raise SystemExit(main())
