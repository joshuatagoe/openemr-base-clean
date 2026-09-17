"""Load test for the agent's briefing path (ARCHITECTURE.md section 13, "Baseline and load-test plan").

One virtual user = POST a signed fixture bundle, mint a ticket, stream the
briefing, DELETE the bundle - the exact sequence the module and panel
perform. Runs at the given concurrency levels and reports p50/p95/p99 of the
full briefing (bundle post to `complete`/`degraded` frame), error rate, and
the count of degraded events without a proper frame (must be zero).

Point it at an agent started with MODEL_PROVIDER=stub for transport
baselines with no spend, or at a real-provider agent for end-to-end numbers.

    uv run python loadtest/run_load.py --base-url http://127.0.0.1:8766 --secret <COPILOT_TICKET_SECRET> --users 10 50 --iterations 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from statistics import median
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, TicketClaims, mint_ticket, sign_body  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "lab_followup.json"


def pct(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    return sorted_values[min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1)))]


async def one_briefing(client: httpx.AsyncClient, base: str, secret: str, context: dict) -> tuple[float, str]:
    """Return (seconds, outcome) where outcome is complete | degraded:<reason> | error:<detail>."""
    ctx = dict(context, correlation_id=str(uuid4()), patient_uuid=str(uuid4()))
    body = json.dumps(ctx).encode()
    ts = int(time.time())
    started = time.perf_counter()
    r = await client.post(f"{base}/v1/bundles", content=body, headers={"Content-Type": "application/json", TIMESTAMP_HEADER: str(ts), SIGNATURE_HEADER: sign_body(secret, body, ts)})
    if r.status_code != 201:
        return time.perf_counter() - started, f"error:bundle_{r.status_code}"
    acc = r.json()
    now = int(time.time())
    token = mint_ticket(secret, TicketClaims(sub=str(uuid4()), puuid=acc["patient_uuid"], bundle_id=acc["bundle_id"], cid=acc["correlation_id"], jti=uuid4(), iat=now, exp=now + 120))
    outcome = "error:no_terminal_frame"
    async with client.stream("GET", f"{base}/v1/briefings/{acc['bundle_id']}", headers={"Authorization": f"Bearer {token}"}) as s:
        if s.status_code != 200:
            return time.perf_counter() - started, f"error:stream_{s.status_code}"
        async for line in s.aiter_lines():
            if line.startswith("event: complete"):
                outcome = "complete"
            elif line.startswith("event: degraded"):
                outcome = "degraded"
            elif outcome == "degraded" and line.startswith("data: "):
                outcome = "degraded:" + json.loads(line[6:]).get("reason_code", "?")
    elapsed = time.perf_counter() - started
    await client.delete(f"{base}/v1/bundles/{acc['bundle_id']}", headers={"Authorization": f"Bearer {token}"})
    return elapsed, outcome


async def run_level(base: str, secret: str, users: int, iterations: int, context: dict) -> dict:
    results: list[tuple[float, str]] = []

    async def vu() -> None:
        async with httpx.AsyncClient(timeout=60) as client:
            for _ in range(iterations):
                try:
                    results.append(await one_briefing(client, base, secret, context))
                except Exception as exc:  # noqa: BLE001
                    results.append((0.0, f"error:{type(exc).__name__}"))

    wall = time.perf_counter()
    await asyncio.gather(*(vu() for _ in range(users)))
    wall = time.perf_counter() - wall
    times = sorted(t for t, o in results if o == "complete")
    errors = [o for _, o in results if o.startswith("error")]
    degraded = [o for _, o in results if o.startswith("degraded")]
    return {
        "users": users,
        "iterations_per_user": iterations,
        "requests": len(results),
        "complete": len(times),
        "degraded": len(degraded),
        "errors": len(errors),
        "error_rate": round(len(errors) / len(results), 4) if results else 0.0,
        "p50_s": round(pct(times, 0.5), 3),
        "p95_s": round(pct(times, 0.95), 3),
        "p99_s": round(pct(times, 0.99), 3),
        "max_s": round(times[-1], 3) if times else 0.0,
        "median_s": round(median(times), 3) if times else 0.0,
        "throughput_per_s": round(len(results) / wall, 2) if wall else 0.0,
        "error_kinds": sorted(set(errors)),
        "degraded_kinds": sorted(set(degraded)),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8766")
    ap.add_argument("--secret", required=True)
    ap.add_argument("--users", nargs="+", type=int, default=[10, 50])
    ap.add_argument("--iterations", type=int, default=5)
    args = ap.parse_args()
    context = json.loads(FIXTURE.read_text(encoding="utf-8"))["context"]
    async with httpx.AsyncClient(timeout=10) as c:
        ready = await c.get(f"{args.base_url}/ready")
        print("ready:", ready.status_code, ready.json().get("dependencies", {}).get("model_provider"))
    for users in args.users:
        print(json.dumps(await run_level(args.base_url, args.secret, users, args.iterations, context)))


if __name__ == "__main__":
    asyncio.run(main())
