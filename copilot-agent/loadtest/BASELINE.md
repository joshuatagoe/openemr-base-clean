# Load baseline — agent briefing path

Recorded 2026-09-17 with `loadtest/run_load.py` against one uvicorn process on a
developer laptop (Windows 11, Python 3.12, `MODEL_PROVIDER=stub`, in-memory
store). Each virtual user runs the full sequence the module and panel perform:
signed `POST /v1/bundles` → mint ticket → `GET /v1/briefings/{id}` (SSE to the
terminal frame) → `DELETE /v1/bundles/{id}`. Five iterations per user.

| Users | Requests | Complete | Degraded | Errors | p50 | p95 | p99 | max | Throughput |
|---|---|---|---|---|---|---|---|---|---|
| 10 | 50 | 50 | 0 | 0 | 0.048 s | 0.134 s | 0.160 s | 0.160 s | 103 /s |
| 50 | 250 | 250 | 0 | 0 | 0.244 s | 0.733 s | 0.807 s | 0.826 s | 111 /s |

Pass criteria (ARCHITECTURE.md §13): 10 users p95 ≤ 8 s and errors < 1 % — **pass**;
50 users no timeouts without a degraded frame and errors < 5 % — **pass**.

Server-side (`/metrics` after the run): `briefing` span p50 3 ms, p95 4 ms,
p99 5 ms — the transport, store, matcher and verifier are not the bottleneck.

## Real-provider numbers (single briefings, `claude-opus-5`, effort `low`)

Measured on the seeded patient during the browser checks (one briefing = one
extraction call, ~4.4–5.0 s end to end; one follow-up turn with two parallel
tool calls ≈ 18.7 s). These sit inside the ≤ 8 s p95 briefing target and outside
the ≤ 4 s follow-up target; see the README's tuning notes (`MODEL_ID_TURN`,
`TURN_EFFORT`).

## OpenEMR-side observation

`GET /apis/default/fhir/metadata` on the development-easy stack (Xdebug and the
profiler enabled) answers in ~5.5 s; `/ready` therefore uses a 5 s default
probe timeout (`COPILOT_READY_PROBE_TIMEOUT_SECONDS`). The module's ticket route
was not load-tested in this pass (it needs authenticated sessions). Per
ARCHITECTURE.md §13, OpenEMR — not the agent — is the scaling
constraint; run the module-side load test (authenticated sessions against
`POST /api/copilot/briefing-ticket`) on a stack with Xdebug off before drawing
conclusions.

## Reproduce

```
# terminal 1: stub agent, no spend
COPILOT_TICKET_SECRET=<32+ chars> MODEL_PROVIDER=stub uv run uvicorn app.main:app --port 8766
# terminal 2
uv run python loadtest/run_load.py --base-url http://127.0.0.1:8766 --secret <same secret> --users 10 50 --iterations 5
# or, with k6 installed
k6 run -e BASE_URL=http://127.0.0.1:8766 -e SECRET=<same secret> --vus 10 --iterations 50 loadtest/k6_briefing.js
```

Point the same runner at a real-provider agent for end-to-end numbers (this
spends money: roughly $0.03 per briefing on `claude-opus-5`).
