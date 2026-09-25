"""Test-name aliases that labs print in reversed, comma-separated order.

Found 2026-09-25 by the Week 2 contract smoke test: a chart row printed "Glucose, Fasting"
(no LOINC code) did not resolve, so a follow-up could say "No glucose result was found."
while the chart held one.
"""

from __future__ import annotations

import pytest

from app.synonyms import resolve_record


@pytest.mark.parametrize("printed", ["Glucose, Fasting", "GLUCOSE, SERUM", "Glucose, Plasma", "Glucose"])
def test_reversed_glucose_names_resolve_without_a_code(printed: str) -> None:
    assert resolve_record(printed, None) == "glucose"


def test_a_code_still_wins_over_the_name() -> None:
    assert resolve_record("Glucose, Fasting", "4548-4") == "hba1c"
