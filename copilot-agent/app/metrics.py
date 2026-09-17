"""In-process metrics (ARCHITECTURE.md section 14, "Metrics").

Counters, latency histograms (p50/p95/p99 over a bounded reservoir) and
token/cost totals, exposed on ``/metrics`` as JSON. Labels are identifiers
and outcome codes only - never clinical values, prompts or patient ids. This
is the process-local view; the self-hosted Langfuse deployment (deferred)
receives the same numbers as trace attributes through the ``span`` seam.

Cost is an estimate from a configurable per-million-token price table; it is
reported as an estimate and never used for decisions.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

RESERVOIR = 512

# USD per million tokens, by model id prefix (longest prefix wins). Configurable through ModelSettings.
DEFAULT_PRICES: dict[str, tuple[float, float, float]] = {
    # (input, cached input, output)
    "claude-opus-5": (5.0, 0.5, 25.0),
    "claude-sonnet-5": (3.0, 0.3, 15.0),
    "claude-haiku-4-5": (1.0, 0.1, 5.0),
}


def estimate_cost_usd(model: str, input_tokens: int, cached_tokens: int, output_tokens: int, prices: dict[str, tuple[float, float, float]] | None = None) -> float:
    table = prices or DEFAULT_PRICES
    match = max((k for k in table if model.startswith(k)), key=len, default=None)
    if match is None:
        return 0.0
    p_in, p_cached, p_out = table[match]
    uncached = max(input_tokens - cached_tokens, 0)
    return (uncached * p_in + cached_tokens * p_cached + output_tokens * p_out) / 1_000_000


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return sorted_values[idx]


@dataclass
class Metrics:
    counters: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    latencies: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=RESERVOIR)))
    tokens: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    cost_usd: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, name: str, **labels: Any) -> None:
        key = name + ("{" + ",".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}" if labels else "")
        with self._lock:
            self.counters[key] += 1

    def observe(self, name: str, duration_ms: float) -> None:
        with self._lock:
            self.latencies[name].append(float(duration_ms))

    def add_usage(self, model: str, input_tokens: int | None, cached_tokens: int | None, output_tokens: int | None, prices: dict[str, tuple[float, float, float]] | None = None) -> float:
        i, c, o = input_tokens or 0, cached_tokens or 0, output_tokens or 0
        cost = estimate_cost_usd(model, i, c, o, prices)
        with self._lock:
            self.tokens["input"] += i
            self.tokens["cached_input"] += c
            self.tokens["output"] += o
            self.tokens["requests"] += 1
            self.cost_usd += cost
        return cost

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lat = {}
            for name, values in self.latencies.items():
                s = sorted(values)
                lat[name] = {"count": len(s), "p50_ms": _percentile(s, 0.50), "p95_ms": _percentile(s, 0.95), "p99_ms": _percentile(s, 0.99), "max_ms": s[-1] if s else 0.0}
            return {
                "counters": dict(sorted(self.counters.items())),
                "latency": lat,
                "tokens": dict(self.tokens),
                "estimated_cost_usd": round(self.cost_usd, 6),
            }

    def reset(self) -> None:
        with self._lock:
            self.counters.clear()
            self.latencies.clear()
            self.tokens.clear()
            self.cost_usd = 0.0


metrics = Metrics()

__all__ = ["DEFAULT_PRICES", "Metrics", "estimate_cost_usd", "metrics"]
