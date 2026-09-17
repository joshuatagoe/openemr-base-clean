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
