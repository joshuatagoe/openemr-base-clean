"""Alert rules over the self-hosted Langfuse Metrics API v2 (ARCHITECTURE.md section 14; OBSERVABILITY_GUIDE.md Part F).

The four rules are native Langfuse v4 alert rules (the alerting channel);
this script is their versioned, tested copy for on-demand checks from a
terminal: it evaluates the four rules over their trailing windows, prints
one line per rule with the on-call response on a breach, posts breaches to
``ALERT_WEBHOOK_URL`` when set (Slack-compatible ``{"text": ...}``), and
exits non-zero on any breach so it can also run under any scheduler.

Only aggregates leave Langfuse (counts, percentiles, averages); no trace
content is read. "No data" in a window is OK - quiet clinic hours are not an
outage.

Environment: ``LANGFUSE_BASE_URL``, ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``;
optional ``ALERT_WEBHOOK_URL``. Standard library only so a CI runner needs no install.
Uses ``/api/public/v2/metrics`` (the v1 endpoint is unavailable in v4 ``events_only`` mode).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# --------------------------------------------------------------------------- #
# Rules (thresholds and windows from ARCHITECTURE.md section 14)
# --------------------------------------------------------------------------- #

RUNBOOK = {
    "briefing_latency": "Compare OpenEMR vs LLM time in the trace (briefing span vs extract generation); if the model is slow, lower EXTRACTION_EFFORT or raise the semaphore; if not, inspect the module's service-read timings.",
    "degraded_rate": "Check /ready on both services, the last deploy and provider status; briefings_degraded{reason} in /metrics names the failure class; roll back if deploy-correlated.",
    "tool_failure_rate": "Inspect the failing source in module logs (service exceptions, ACL); verify database health; source_unavailable means a clinical read failed, unresolvable_query means the model asked for something the synonym table cannot resolve.",
    "hallucinated_span": "Page. A statement or span was withheld by the verifier: the guardrail worked, but the model proposed ungrounded content. Disable the agent path via the module global while investigating the trace.",
}


@dataclass(frozen=True)
class Rule:
    key: str
    title: str
    window_minutes: int
    threshold: float
    comparison: str  # ">" or ">="
    unit: str
    warn: float | None = None


RULES: tuple[Rule, ...] = (
    Rule("briefing_latency", "briefing p95 latency", 5, 8000, ">", "ms", warn=6000),
    Rule("degraded_rate", "degraded rate (briefings and turns)", 5, 0.05, ">", "ratio"),
    Rule("tool_failure_rate", "tool failure rate", 10, 0.10, ">", "ratio"),
    Rule("hallucinated_span", "hallucinated spans withheld", 5, 0, ">", "count"),
)


@dataclass(frozen=True)
class Result:
    rule: Rule
    value: float | None  # None: no data in the window
    samples: int

    @property
    def breached(self) -> bool:
        if self.value is None:
            return False
        return self.value > self.rule.threshold if self.rule.comparison == ">" else self.value >= self.rule.threshold

    @property
    def warning(self) -> bool:
        return self.value is not None and self.rule.warn is not None and not self.breached and self.value > self.rule.warn

    @property
    def status(self) -> str:
        return "BREACH" if self.breached else "WARN" if self.warning else "ok"

    def line(self) -> str:
        shown = "no data" if self.value is None else (f"{self.value:.0f} {self.rule.unit}" if self.rule.unit == "ms" else f"{self.value:.3f}" if self.rule.unit == "ratio" else f"{self.value:.0f}")
        return f"[{self.status:<6}] {self.rule.title}: {shown} over {self.rule.window_minutes} min (n={self.samples}; alert {self.rule.comparison} {self.rule.threshold})"


# --------------------------------------------------------------------------- #
# Metrics API
# --------------------------------------------------------------------------- #


class MetricsClient:
    def __init__(self, base_url: str, public_key: str, secret_key: str, timeout: float = 20.0) -> None:
        self._url = base_url.rstrip("/") + "/api/public/v2/metrics"
        self._auth = "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._timeout = timeout

    def query(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        url = self._url + "?" + urllib.parse.urlencode({"query": json.dumps(query)})
        req = urllib.request.Request(url, headers={"Authorization": self._auth, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 - https URL from configuration
            return json.load(resp)["data"]


def _window(minutes: int, now: datetime) -> dict[str, str]:
    start = now - timedelta(minutes=minutes)
    return {"fromTimestamp": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "toTimestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _num(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    return 0.0 if value is None else float(value)


def evaluate(rule: Rule, query: Any, now: datetime) -> Result:
    """Evaluate one rule; ``query`` is ``MetricsClient.query`` or a stand-in returning rows."""
    win = _window(rule.window_minutes, now)
    if rule.key == "briefing_latency":
        rows = query({"view": "observations", "metrics": [{"measure": "latency", "aggregation": "p95"}, {"measure": "count", "aggregation": "count"}], "dimensions": [], "filters": [{"column": "name", "operator": "=", "value": "briefing", "type": "string"}], **win})
        n = int(_num(rows[0], "count_count")) if rows else 0
        return Result(rule, _num(rows[0], "p95_latency") if n else None, n)
    if rule.key == "degraded_rate":
        rows = query({"view": "scores-numeric", "metrics": [{"measure": "value", "aggregation": "avg"}, {"measure": "count", "aggregation": "count"}], "dimensions": [], "filters": [{"column": "name", "operator": "=", "value": "degraded", "type": "string"}], **win})
        n = int(_num(rows[0], "count_count")) if rows else 0
        return Result(rule, _num(rows[0], "avg_value") if n else None, n)
    if rule.key == "tool_failure_rate":
        rows = query({"view": "observations", "metrics": [{"measure": "count", "aggregation": "count"}], "dimensions": [{"field": "level"}], "filters": [{"column": "name", "operator": "=", "value": "tool", "type": "string"}], **win})
        total = sum(int(_num(r, "count_count")) for r in rows)
        errors = sum(int(_num(r, "count_count")) for r in rows if r.get("level") == "ERROR")
        return Result(rule, errors / total if total else None, total)
    if rule.key == "hallucinated_span":
        rows = query({"view": "scores-numeric", "metrics": [{"measure": "value", "aggregation": "sum"}, {"measure": "count", "aggregation": "count"}], "dimensions": [], "filters": [{"column": "name", "operator": "=", "value": "hallucinated_span", "type": "string"}], **win})
        n = int(_num(rows[0], "count_count")) if rows else 0
        return Result(rule, _num(rows[0], "sum_value") if n else None, n)
    raise ValueError(rule.key)


def notify(webhook_url: str, results: list[Result]) -> None:
    breaches = [r for r in results if r.breached]
    text = "Clinical Co-Pilot alert\n" + "\n".join(f"{r.line()}\n  → {RUNBOOK[r.rule.key]}" for r in breaches)
    req = urllib.request.Request(webhook_url, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).close()  # noqa: S310


def main(argv: list[str] | None = None) -> int:
    base_url, public_key, secret_key = (os.environ.get(k, "").strip() for k in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"))
    if not (base_url and public_key and secret_key):
        print("LANGFUSE_BASE_URL, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required", file=sys.stderr)
        return 2
    client = MetricsClient(base_url, public_key, secret_key)
    now = datetime.now(UTC)
    results: list[Result] = []
    for rule in RULES:
        try:
            results.append(evaluate(rule, client.query, now))
        except (urllib.error.URLError, KeyError, ValueError, json.JSONDecodeError) as exc:
            print(f"[ERROR ] {rule.title}: metrics query failed ({type(exc).__name__})", file=sys.stderr)
            return 2
    for r in results:
        print(r.line())
        if r.breached:
            print(f"         → {RUNBOOK[r.rule.key]}")
    breached = any(r.breached for r in results)
    webhook = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if breached and webhook:
        try:
            notify(webhook, results)
        except urllib.error.URLError as exc:
            print(f"webhook notification failed ({type(exc).__name__})", file=sys.stderr)
    return 1 if breached else 0


if __name__ == "__main__":
    sys.exit(main())
