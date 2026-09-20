# Load baseline — agent briefing path

Two tiers of numbers: the **deployed agent with the real model** (the brief's requirement:
10 and 50 concurrent users against the deployed service, p50/p95/p99, error rate, CPU and
memory), and the earlier **stub-provider transport baseline** on a laptop, kept for comparison.
Runner: `loadtest/run_load.py`. Each virtual user performs the exact sequence the module and
panel perform — signed `POST /v1/bundles` → mint ticket → `GET /v1/briefings/{id}` (SSE to the
terminal frame) → `DELETE /v1/bundles/{id}` — five times, back to back, with no think time.

## 1. Deployed agent, real model (Railway, `claude-opus-5`, effort `low`, 2026-09-20)

Target: `https://copilot-agent-production-0395.up.railway.app`, one replica.

### Final numbers (defaults now in the repo: concurrency 16, queue wait 3 s, retry budget 3 s, 3 attempts)

| Users | Requests | Complete | Degraded | Errors | p50 | p95 | p99 | max | Throughput |
|---|---|---|---|---|---|---|---|---|---|
| 10 | 50 | 49 | 1 (`timeout`) | 0 | 2.94 s | 6.52 s | 7.26 s | 7.26 s | 2.1 /s |
| 50 | 250 | 141 | 104 `provider_busy`, 5 `timeout` | 0 | 5.09 s | 7.34 s | 9.97 s | 10.05 s | 7.8 /s |

Pass criteria (ARCHITECTURE.md §13): 10 users p95 ≤ 8 s and errors < 1 % — **pass**;
50 users: no silent failures (every non-completion is an explicit `degraded` frame with a
reason code), errors < 5 % — **pass**. Every request returned HTTP 200 with a terminal frame;
"errors" (transport failures, malformed frames, missing terminal frame) were zero in every run.

**Infrastructure during the runs** (Railway metrics, agent service): peak **0.2 vCPU** of a
24 vCPU limit; memory **253 MB** (from ~100 MB idle) of 24 GB; 5 MB egress / 3 MB ingress for the
two runs; ~900 requests in the 15-minute window. The agent process is not the constraint at
this load: its own `briefing` span p50 stayed at ~2–3 s, and the model call dominates.

### What the 50-user run measures, and the two iterations it took

"50 concurrent users" here means 50 physicians each requesting five briefings back to back:
250 briefings in ~30 s ≈ **500–700 per minute**. In clinic terms (COST_ANALYSIS.md §4: 14
briefings per physician-day, 8-hour day, 3× peak) that is the peak minute of roughly
**8,000 physicians**; a 500-bed hospital with 300 concurrent clinical users peaks at about
**26 briefings per minute**, which the 10-user row already exceeds. The 50-user burst is
therefore a stress test of what fails first — and the answer is the model provider's rate
limit, not the agent.

| 50 users, 250 briefings | Run 1 — as first deployed | Run 2 — naive gate (8 slots, unbounded wait) | Run 3 — bounded gate (16, 4 s) | Final — 16 slots, 3 s wait, 3 s retry budget |
|---|---|---|---|---|
| Complete | 129 | 72 | 156 | 141 |
| Degraded | 121 `provider_rate_limited` | 178 `timeout` | 84 `provider_busy`, 10 `timeout` | 104 `provider_busy`, 5 `timeout` |
| p50 / p95 / p99 | 3.0 / 5.0 / 7.8 s | 9.4 / 10.7 / 14.5 s | 6.1 / 8.3 / 9.0 s | 5.1 / 7.3 / 10.0 s |
| Throughput | 11.7 /s (mostly failures) | 4.3 /s | 6.9 /s | 7.8 /s |

- **Run 1** exposed a failure-handling flaw, not a capacity one: the extractor retried a 429
  once, immediately, without `Retry-After`, so under a burst a briefing degraded within
  milliseconds while ~8 s of its budget remained.
- **Run 2** showed the wrong fix: a small in-process semaphore with an unbounded wait turned
  fast, explicit degrades into silent waits to the 10 s timeout — the worst outcome for a
  physician. Reverted the same hour.
- **Run 3 / final**: every model call passes a gate sized to the provider's observed throughput
  (~6 calls/s × ~3 s ≈ 16 in flight) with a **bounded** wait; a request that cannot get a slot in
  3 s degrades at once with `provider_busy` (503 + `Retry-After` on the sync path); retryable
  provider errors back off exponentially or per `Retry-After` inside a 3 s budget; queue wait +
  retry budget + one model call stay under the 10 s request timeout by construction. No request
  reached the provider's rate limit in runs 3 and final; the remaining timeouts are single slow
  model calls (the provider's latency tail, also visible once in the 10-user run).

The point of the change is graceful degradation, not throughput: at clinic load the gate is
never full and nothing waits. The completion count at this burst is bounded by the provider
tier, and the code cannot change that.

### Handling this load for real (not built; recorded so the numbers have a plan)

If ~700 briefings/min were a real requirement — the 10K-physician tier in COST_ANALYSIS.md §5 —
the changes are architectural, in this order:

1. **Pre-visit precomputation** (ARCHITECTURE.md ARCH-005): extract commitments overnight for
   tomorrow's schedule and cache by note id; the visit-time briefing then runs only the
   deterministic match (sub-second, zero tokens). Removes the model from the peak entirely and
   makes the overnight batch eligible for batch pricing.
2. **Provider capacity**: an enterprise rate-limit tier or provisioned throughput, sized to the
   live residual (same-day notes and follow-up turns).
3. **Horizontal agent replicas** behind Railway's load balancer once the bundle store moves to
   Redis (`store.py` has the seam); the gate then bounds per-replica concurrency and the queue
   depth on `/metrics` (`provider_queue.waiting`, `rejected`) drives autoscaling.
4. **Cheaper extraction model** if the eval gates hold (KEY_METRICS.md §4): ×0.6 (Sonnet) or
   ×0.2 (Haiku) of the cost, and typically lower latency per call, which raises the gate's
   effective throughput.

## 2. Stub provider, laptop (transport baseline, 2026-09-17)

One uvicorn process on a developer laptop (Windows 11, Python 3.12, `MODEL_PROVIDER=stub`,
in-memory store): the same sequence with no model call, isolating transport, store, matcher
and verifier.

| Users | Requests | Complete | Degraded | Errors | p50 | p95 | p99 | max | Throughput |
|---|---|---|---|---|---|---|---|---|---|
| 10 | 50 | 50 | 0 | 0 | 0.048 s | 0.134 s | 0.160 s | 0.160 s | 103 /s |
| 50 | 250 | 250 | 0 | 0 | 0.244 s | 0.733 s | 0.807 s | 0.826 s | 111 /s |

Server-side `briefing` span p50 3 ms, p95 4 ms, p99 5 ms: everything except the model call is
negligible.

## 3. OpenEMR side (not load-tested)

`GET /apis/default/fhir/metadata` on the development-easy stack (Xdebug and the profiler
enabled) answers in ~5.5 s; `/ready` therefore uses a 5 s default probe timeout
(`COPILOT_READY_PROBE_TIMEOUT_SECONDS`). The module's ticket route
(`POST /api/copilot/briefing-ticket`) needs authenticated OpenEMR sessions and was not
load-tested; per ARCHITECTURE.md §13, OpenEMR — ACL checks, audit-row volume, sessions — is
the expected constraint before the agent is, and the authenticated module-side test on a stack
with Xdebug off remains the open item in §17.

## Reproduce

```sh
# deployed, real model (about $1 for both levels at the measured $0.004 per briefing)
uv run python loadtest/run_load.py --base-url https://copilot-agent-production-0395.up.railway.app --secret <COPILOT_TICKET_SECRET> --users 10 50 --iterations 5

# local transport baseline, no spend
COPILOT_TICKET_SECRET=<32+ chars> MODEL_PROVIDER=stub uv run uvicorn app.main:app --port 8766
uv run python loadtest/run_load.py --base-url http://127.0.0.1:8766 --secret <same> --users 10 50 --iterations 5
```

Read CPU and memory from the Railway service's Metrics tab for the run window; `/metrics` on
the agent gives the server-side span percentiles, the degraded breakdown by reason and the
provider queue (`concurrency`, `waiting`, `rejected`).
