"""Alert rules over the Langfuse Metrics API (ops/langfuse_alerts.py): thresholds, windows, no-data handling."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ops import langfuse_alerts as alerts

NOW = datetime(2026, 9, 19, 20, 0, 0, tzinfo=UTC)
RULES = {r.key: r for r in alerts.RULES}


def _query_returning(rows: list[dict[str, Any]], seen: list[dict[str, Any]] | None = None):
    def query(q: dict[str, Any]) -> list[dict[str, Any]]:
        if seen is not None:
            seen.append(q)
        return rows

    return query


def test_briefing_latency_breaches_above_8s_and_warns_above_6s() -> None:
    rule = RULES["briefing_latency"]
    seen: list[dict[str, Any]] = []
    r = alerts.evaluate(rule, _query_returning([{"p95_latency": 8500.0, "count_count": "12"}], seen), NOW)
    assert r.breached and r.status == "BREACH" and r.samples == 12
    assert seen[0]["filters"] == [{"column": "name", "operator": "=", "value": "briefing", "type": "string"}]
    assert seen[0]["fromTimestamp"] == "2026-09-19T19:55:00Z" and seen[0]["toTimestamp"] == "2026-09-19T20:00:00Z"
    warn = alerts.evaluate(rule, _query_returning([{"p95_latency": 6500.0, "count_count": "3"}]), NOW)
    assert not warn.breached and warn.status == "WARN"
    ok = alerts.evaluate(rule, _query_returning([{"p95_latency": 3400.0, "count_count": "3"}]), NOW)
    assert ok.status == "ok"


def test_no_data_in_window_is_ok_not_a_breach() -> None:
    for key in RULES:
        r = alerts.evaluate(RULES[key], _query_returning([]), NOW)
        assert r.value is None and not r.breached and r.status == "ok", key
        assert "no data" in r.line()
    zero = alerts.evaluate(RULES["briefing_latency"], _query_returning([{"p95_latency": None, "count_count": "0"}]), NOW)
    assert zero.value is None and not zero.breached


def test_degraded_rate_uses_the_degraded_score_average() -> None:
    r = alerts.evaluate(RULES["degraded_rate"], _query_returning([{"avg_value": 0.08, "count_count": "25"}]), NOW)
    assert r.breached and r.value == pytest.approx(0.08)
    r = alerts.evaluate(RULES["degraded_rate"], _query_returning([{"avg_value": 0.05, "count_count": "20"}]), NOW)
    assert not r.breached  # threshold is strictly greater than


def test_tool_failure_rate_is_error_level_over_all_tool_spans() -> None:
    rows = [{"level": "DEFAULT", "count_count": "18"}, {"level": "ERROR", "count_count": "2"}]
    r = alerts.evaluate(RULES["tool_failure_rate"], _query_returning(rows), NOW)
    assert r.value == pytest.approx(0.1) and not r.breached and r.samples == 20
    rows = [{"level": "DEFAULT", "count_count": "8"}, {"level": "ERROR", "count_count": "2"}]
    assert alerts.evaluate(RULES["tool_failure_rate"], _query_returning(rows), NOW).breached
    assert RULES["tool_failure_rate"].window_minutes == 10


def test_any_hallucinated_span_is_a_breach() -> None:
    r = alerts.evaluate(RULES["hallucinated_span"], _query_returning([{"sum_value": 1.0, "count_count": "12"}]), NOW)
    assert r.breached
    r = alerts.evaluate(RULES["hallucinated_span"], _query_returning([{"sum_value": 0.0, "count_count": "12"}]), NOW)
    assert not r.breached


def test_main_exits_nonzero_on_breach_and_two_without_credentials(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    for k in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    assert alerts.main() == 2

    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.example")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    answers = {
        "briefing": [{"p95_latency": 3000.0, "count_count": "4"}],
        "degraded": [{"avg_value": 0.0, "count_count": "4"}],
        "tool": [{"level": "DEFAULT", "count_count": "3"}],
        "hallucinated_span": [{"sum_value": 1.0, "count_count": "4"}],
    }

    def fake_query(self: Any, q: dict[str, Any]) -> list[dict[str, Any]]:
        return answers[q["filters"][0]["value"]]

    monkeypatch.setattr(alerts.MetricsClient, "query", fake_query)
    assert alerts.main() == 1
    out = capsys.readouterr().out
    assert "[BREACH] hallucinated spans withheld" in out and "Page." in out
    assert "[ok    ] briefing p95 latency" in out

    answers["hallucinated_span"] = [{"sum_value": 0.0, "count_count": "4"}]
    assert alerts.main() == 0


def test_runbook_covers_every_rule() -> None:
    assert set(alerts.RUNBOOK) == set(RULES)
