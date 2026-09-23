"""Guards on the CI gate itself.

The gate is the thing that catches everything else, so a silent failure here is
the most expensive kind: it reports success either way and nobody looks.

Both bugs pinned below were real. The dirty-flag one shipped and was found by
reading a committed baseline, not by any test.
"""

from __future__ import annotations

import subprocess
import sys
from fractions import Fraction
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import eval_gate  # noqa: E402


def test_dirty_flag_distinguishes_clean_from_modified(tmp_path: Path, monkeypatch) -> None:
    """A clean tree must report "no".

    Regression: `_git()` used `stdout.strip() or "unknown"`, and `git status
    --porcelain` returns EMPTY for a clean tree. The falsy empty string fell
    through to the truthy placeholder, so every run reported dirty - including
    the baseline committed to the repo. A flag that is always "yes" looks like
    evidence while carrying none.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, check=True)
    run("init", "-q")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    (repo / "f.txt").write_text("one")
    run("add", "-A")
    run("commit", "-qm", "init")

    monkeypatch.setattr(eval_gate, "REPO", repo)

    assert eval_gate._run_versions()["dirty"] == "no", "a clean tree must not report dirty"

    (repo / "f.txt").write_text("two")
    assert eval_gate._run_versions()["dirty"] == "yes", "a modified tree must report dirty"


def test_thresholds_and_tolerance_are_exact_not_float() -> None:
    """Regression: float rates plus a 1e-9 epsilon decided boundary cases by rounding.

    23/24 has no finite binary representation. A gate that fires or not depending
    on IEEE-754 rounding is not a gate.
    """
    assert isinstance(eval_gate.MAX_DROP, Fraction)
    assert all(isinstance(v, Fraction) for v in eval_gate.THRESHOLDS.values())
    assert eval_gate.MAX_DROP == Fraction(5, 100)


def test_every_rubric_category_has_a_threshold() -> None:
    """A category with no floor is ungated, and nothing would say so."""
    from app.rubrics import CATEGORIES

    assert set(eval_gate.THRESHOLDS) == set(CATEGORIES)
