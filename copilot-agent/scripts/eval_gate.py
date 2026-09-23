#!/usr/bin/env python
"""The Week 2 eval gate (PRD CR6).

Runs the golden set offline, scores five boolean rubrics, and compares the
result against a committed baseline. Exits non-zero if any category regresses
by more than 5 percentage points or falls below its floor.

The logic lives here rather than in .gitlab-ci.yml so that a grader can run it
from a fresh clone with no CI, no runner and no API key:

    uv sync && uv run python scripts/eval_gate.py

Offline by design: the golden set is scripted model output replayed through
the real grounding, matching and verification code. No provider is called, so
a failure is a change in our logic, never sampling noise. See EVAL_GATE.md.

    --update-baseline   rewrite evals/baseline.json from this run
    --json PATH         also write machine-readable results
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.eval import load_cases, run_case  # noqa: E402
from app.rubrics import CATEGORIES, aggregate, score_case  # noqa: E402

BASELINE_PATH = REPO / "evals" / "baseline.json"

# Regression tolerance, from CR6: "fail if any category regresses by more than 5%".
# Exact, not 0.05 as a float: every comparison below is rational arithmetic on
# case counts, so a rate sitting exactly on the boundary is decided by the rule
# rather than by binary rounding (F15).
MAX_DROP = Fraction(5, 100)

# Floors. Four categories are invariants rather than quality targets: a single
# uncited claim or one leaked identifier is a defect, not a percentage. They sit
# at 1 deliberately. factually_consistent is the one with headroom, because it
# is the category a real model regression would move first.
# PROPOSED_DECISION - the PRD does not state thresholds (W2-AMB-005/006).
THRESHOLDS: dict[str, Fraction] = {
    "schema_valid": Fraction(1),
    "citation_present": Fraction(1),
    "factually_consistent": Fraction(95, 100),
    "safe_refusal": Fraction(1),
    "no_phi_in_logs": Fraction(1),
}


def _run_versions() -> dict[str, str]:
    """Identity of this run, so two results are comparable and a diff is attributable (F14).

    Without this, a rate change cannot be told apart from a fixture change, a
    prompt change or a different commit.
    """
    def _git(*args: str) -> str | None:
        """Raw stdout, or None when git could not answer.

        Deliberately does NOT substitute a placeholder for empty output: `git
        status --porcelain` returns empty for a CLEAN tree, and an `or "unknown"`
        fallback here made every run report dirty, since a non-empty placeholder
        is truthy. A flag that is always "yes" is worse than no flag - it looks
        like evidence while carrying none.
        """
        try:
            done = subprocess.run(
                ["git", *args], cwd=REPO, capture_output=True, text=True, timeout=10
            )
            return done.stdout.strip() if done.returncode == 0 else None
        except Exception:
            return None

    def _digest(*paths: Path) -> str:
        h = hashlib.sha256()
        for p in sorted(paths):
            h.update(p.read_bytes())
        return h.hexdigest()[:16]

    cases = sorted((REPO / "fixtures" / "cases").glob("*.json"))
    prompts = REPO / "app" / "providers" / "prompt.py"

    status = _git("status", "--porcelain")
    dirty = "unknown" if status is None else ("yes" if status else "no")

    return {
        "commit": _git("rev-parse", "--short", "HEAD") or "unknown",
        "dirty": dirty,
        "fixture_set": f"{len(cases)} cases / {_digest(*cases)}",
        "prompt_version": _digest(prompts) if prompts.exists() else "unknown",
        "judge": "none (all rubrics deterministic)",
    }


class _Capture(logging.Handler):
    """Collects everything the run logs, so no_phi_in_logs is an exact string test."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.buf = io.StringIO()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buf.write(self.format(record) + "\n")
        except Exception:  # pragma: no cover - a broken log record must not fail the gate silently
            self.buf.write(str(record.msg) + "\n")


def run() -> tuple[dict[str, object], int]:
    cases = load_cases()
    if not cases:
        print("FAIL: no golden cases found", file=sys.stderr)
        return {}, 1

    root = logging.getLogger()
    capture = _Capture()
    previous_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        rows = []
        failed_cases = []
        for case in cases:
            before = capture.buf.tell()
            result = run_case(case)
            capture.buf.seek(before)
            case_log = capture.buf.read()
            rows.append(score_case(case, result, case_log))
            if not result.passed:
                failed_cases.append((case.name, result.failures))
    finally:
        root.removeHandler(capture)
        root.setLevel(previous_level)

    rates = aggregate(rows)
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8")) if BASELINE_PATH.exists() else None
    # Counts, not rates: reconstructing the fraction from integers keeps the
    # comparison exact even when the rate has no finite binary representation.
    base_counts: dict[str, dict[str, int]] = (baseline or {}).get("counts", {})

    violations: list[str] = []
    print(f"\n  golden cases: {len(cases)}\n")
    print(f"  {'category':<22}{'rate':>8}{'base':>8}{'floor':>8}   n")
    print(f"  {'-' * 54}")
    for category in CATEGORIES:
        cr = rates[category]
        floor = THRESHOLDS[category]
        bc = base_counts.get(category)
        base = Fraction(bc["passed"], bc["applicable"]) if bc and bc["applicable"] else None

        flags = []
        if cr.rate < floor:
            flags.append(f"below floor {float(floor):.2f}")
        if base is not None and cr.rate < base - MAX_DROP:
            flags.append(f"regressed >{float(MAX_DROP):.0%} from {float(base):.2f}")
        if flags:
            violations.append(f"{category}: {'; '.join(flags)}")
        base_txt = f"{float(base):.2f}" if base is not None else "  --"
        mark = "FAIL" if flags else "ok"
        print(f"  {category:<22}{float(cr.rate):>8.2f}{base_txt:>8}{float(floor):>8.2f}{cr.applicable:>4}   {mark}")

    if failed_cases:
        print("\n  failing cases:")
        for name, failures in failed_cases:
            print(f"    {name}")
            for f in failures[:4]:
                print(f"      - {f}")

    report = {
        "cases": len(cases),
        "versions": _run_versions(),
        "counts": {c: {"passed": rates[c].passed, "applicable": rates[c].applicable} for c in CATEGORIES},
        "rates": {c: float(rates[c].rate) for c in CATEGORIES},  # display only; counts are authoritative
        "thresholds": {c: float(v) for c, v in THRESHOLDS.items()},
        "max_drop": float(MAX_DROP),
        "violations": violations,
    }

    if violations:
        print("\n  GATE FAILED")
        for v in violations:
            print(f"    - {v}")
        print()
        return report, 1
    print("\n  GATE PASSED\n")
    return report, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-baseline", action="store_true", help="rewrite evals/baseline.json from this run")
    parser.add_argument("--json", type=Path, default=None, help="also write machine-readable results here")
    args = parser.parse_args(argv)

    report, code = run()
    if not report:
        return code

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if args.update_baseline:
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(
            json.dumps(
                {
                    "cases": report["cases"],
                    "versions": report["versions"],
                    "counts": report["counts"],
                    "rates": report["rates"],  # human-readable mirror; counts are what the gate reads
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"  baseline written to {BASELINE_PATH.relative_to(REPO)}\n")
        return 0
    return code


if __name__ == "__main__":
    raise SystemExit(main())
