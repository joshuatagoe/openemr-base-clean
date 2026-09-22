# AI Cost Analysis — Clinical Co-Pilot

Actual development spend, measured unit costs, and projected production cost at 100 / 1K / 10K / 100K
physicians with the architectural changes each level requires. Unit costs are **measured** from
Langfuse generations on the deployed agent (2026-09-19, `claude-opus-5`, effort `low`), not list-price
arithmetic. Usage assumptions come from the persona in USER.md.

## 1. Summary

- A briefing costs **$0.0042** (one extraction call; ~630 input + ~150 output tokens; p50 2.4 s). A
  follow-up turn costs **$0.0058** (1.7 model steps on average; p50 4.6 s). Deterministic matching,
  verification and the OpenEMR reads cost no tokens.
- One primary-care physician (14 established-patient visits a day, one follow-up question every second
  visit, 21 clinic days) costs **$2.09 / month** in model spend on Opus. That is the whole variable cost;
  everything else is infrastructure that steps, not scales.
- Model spend at 100 / 1K / 10K / 100K physicians: **$209 / $2.1K / $21K / $209K per month** on Opus.
  The design keeps the model out of the hot path where it can (extraction only; matching is code), so
  the remaining lever is the extraction model: Sonnet cuts the bill to 60 %, Haiku to 20 %, and both
  are a configuration change gated by the eval harness, not a rewrite.
- Cost is not the scaling problem. OpenEMR is (ARCHITECTURE.md §13): ACL checks, audit-row volume and
  session storage bind before the agent or the model provider does. The architectural changes per tier
  are mostly on that side.
- Development spend to date: **≈ $13 of Anthropic API usage** (2.07 M input + 0.11 M output tokens on Opus 5, list
  price, an upper bound since cached prefix tokens bill at a tenth), **$200 / month of Claude Code** (Max plan, used
  to build the project; ≈ $40 prorated to the six-day build), **$20 / month of ChatGPT Plus** (supporting research
  and writing; ≈ $4 prorated), and **$20 / month Railway** (Pro plan; the three
  services and the six-service Langfuse stack run inside the plan's included usage). Most of the API tokens were
  spent on 2026-09-20 on the deployed load tests, the live evaluation run and the API-collection checks — the unit
  suite, the deterministic eval tier and the load baseline all run on the stub provider.

## 2. Development spend (actual)

| Item | Amount | Source |
|---|---|---|
| Anthropic API — the agent's model calls during development, evaluation, load tests and verification (Sept 15–20) | ≈ $13 (2,069,958 input + 111,743 output tokens, Opus 5; list price, upper bound) | console.anthropic.com → Usage, last 30 days |
| Claude Code — AI-assisted development of the module, agent, tests and documents | $200 / month plan (≈ $40 prorated to the six-day build) | Max plan; usage not itemised by the plan |
| ChatGPT Plus — supporting research and writing during the build week | $20 / month plan (≈ $4 prorated to the six-day build) | Plan is not metered per project; the usage page shows peak activity in the build week |
| Railway: OpenEMR + agent + Langfuse (postgres, clickhouse, redis, minio, web, worker) | $20 / month | Railway Pro plan; usage within the included credit |
| Traced model calls since Langfuse went live (2026-09-19) | $0.11 | Langfuse: 9 production generations $0.034; earlier v3-era traces $0.075 |
| One 18-case eval run, live tier | ≈ $0.08 | 18 extractions × $0.0042 |
| One Bruno collection run against the deployed agent | ≈ $0.02 | 10 + 21 + 22 (three model calls) |

The API figure is the console total at list price; the traced sub-totals below it are measured per call.
Development spend is dominated by the tooling subscriptions and the human time, not by the agent's tokens:
the stub provider (`MODEL_PROVIDER=stub`) carries the whole unit suite, the deterministic eval tier, the
transport load baseline and the collection test without a model call, and the ~$13 of API usage bought
roughly 1,500 real briefings and turns across the load tests, eval runs and manual verification.

## 3. Measured unit costs

Langfuse Metrics API v2, `environment = production`, generations only:

| Generation | Calls | Cost | Input tokens | Output tokens | p50 latency | Per call |
|---|---|---|---|---|---|---|
| `extract` (one per briefing) | 4 | $0.0169 | 2,504 | 592 | 2.42 s | **$0.0042** |
| `turn_step` (1–3 per follow-up) | 5 | $0.0173 | 7,917 | 517 | 2.53 s | **$0.0035** |

Derived: 3 turns took 5 steps → **1.67 steps per turn → $0.0058 per turn**, p50 4.6 s end to end.
The system prompt sits on the cached prefix (`cache_read_input_tokens` is reported separately on every
generation), so input cost per call is dominated by the plan text and tool results, which are small by
construction (minimum-necessary bundle, bounded tool outputs).

Price table in force (`copilot-agent/app/metrics.py`, USD per million tokens, input / cached / output):
Opus 5 = 5 / 0.5 / 25; Sonnet 5 = 3 / 0.3 / 15; Haiku 4.5 = 1 / 0.1 / 5. Override with `PRICE_*`.

What costs nothing: evidence matching, interval annotation, span grounding and statement verification
are deterministic code; the OpenEMR reads are parameter-bound SQL; the panel renders server-provided
sections without the model. A degraded briefing (provider down) costs zero and still renders the
deterministic sections.

## 4. Usage model

From USER.md: a primary-care physician with a 20-patient day. Assumptions, all adjustable:

| Assumption | Value | Basis |
|---|---|---|
| Visits per physician-day | 20 | USER.md persona |
| Established-patient share (a briefing needs a prior plan) | 70 % → **14 briefings/day** | USER.md §1.1 |
| Follow-up turns per briefing | **0.5** | UC-04 is secondary: the persona does not type in the 90-second window |
| Clinic days per month | 21 | |
| Concurrency shape | 8-hour clinic day, peak minute = 3 × average | morning and post-lunch bunching |

→ **294 briefings + 147 turns per physician-month = $2.09** on Opus.

Sensitivity: the bill is linear in briefings per day and in the turn rate. Doubling follow-up use
(1 turn per briefing) adds $0.85 per physician-month; a 30-patient day adds $0.90. Neither changes the
architecture.

## 5. Projection by scale

| Physicians | Briefings / day | Peak briefings / min | Tokens / month | Model spend / month (Opus) | with Sonnet extraction | with Haiku extraction |
|---|---|---|---|---|---|---|
| 100 | 1,400 | 9 | 64 M | **$209** | $125 | $42 |
| 1,000 | 14,000 | 88 | 641 M | **$2,087** | $1,250 | $420 |
| 10,000 | 140,000 | 875 | 6.4 B | **$20,868** | $12,500 | $4,200 |
| 100,000 | 1,400,000 | 8,750 | 64 B | **$208,679** | $125,000 | $42,000 |

Sonnet/Haiku columns apply the price ratio to the measured token mix (×0.60 and ×0.20); they assume the
eval gates in KEY_METRICS.md §4 (extraction precision/recall, hallucinated-item rate) hold on the cheaper
model, which is the only reason to keep Opus. The harness compares models on the same fixtures with
latency and cost side by side, so this is a measured decision per stage (`MODEL_ID_EXTRACTION`,
`MODEL_ID_TURN`), not a bet.

### Infrastructure and architectural changes per tier

**100 physicians — what runs today.** One agent replica, the OpenEMR instance and the six-service
Langfuse stack, all on the $20/month Railway Pro plan today; at 100 real users expect usage billing of
roughly $50–100/mo on top (the Langfuse volumes and a second agent replica). Peak 9 briefings/min is
nothing. The in-memory bundle store is fine because there is one replica. Cost per physician: ~$3/month
all-in. Nothing to change;
the open items are safety, not scale (BAA on file, Railway TLS/backup verification, `api_log` retention).

**1K physicians — make the agent stateless, fix OpenEMR's hot path.** Peak ~90 briefings/min is well
inside one provider tier, but a single agent replica is now a single point of failure:
- Bundle store → Redis (`store.py` has the seam; TTL semantics unchanged), so agent replicas scale
  horizontally behind Railway's load balancer and a deploy does not drop in-flight tickets.
- OpenEMR (the real constraint): ACL memoization (AUDIT PERF-001), Redis sessions
  (`SESSION_STORAGE_MODE=predis-sentinel`) before adding OpenEMR replicas, indexes on
  `form_clinical_notes(pid, encounter)` and `pc_pid` (PERF-005), audit-write volume (PERF-002).
- Langfuse: ClickHouse volume sizing and a retention policy; one trace per briefing at 14K/day is ~1 GB/month
  of masked events.
- Infra ≈ $300–600/mo; model $2.1K; **~$2.50–3 per physician-month**.

**10K physicians — take the model out of the peak.** Peak ~875 briefings/min (~15/s) with a ~2.5 s model
call means ~40 concurrent provider requests; fine for the agent, but now rate limits, provider incidents
and cost governance matter. This is the tier the 50-user load test actually exercised (~700/min): the
provider's rate limit was the first ceiling, the agent's bounded gate degraded the excess explicitly, and
the measured completion ceiling was ~7 briefings/s on the current tier (`copilot-agent/loadtest/BASELINE.md`):
- **Pre-visit precomputation**: the day's schedule is known; extract commitments
  overnight for tomorrow's established patients and store the extraction keyed by note id. Briefings then
  run only the deterministic match at visit time (sub-second, zero tokens), the model is off the
  latency-critical path, and the overnight batch is eligible for batch pricing (−50 %). Turn traffic stays
  live. Safe because extraction reads only the baseline note, which does not change between the overnight
  run and the visit; anything that must be fresh (a result that arrived this morning) is the deterministic
  match, which is free and runs at visit time. **The scheduler runs in the agent, not in OpenEMR**
  (AUDIT.md ARCH-005: OpenEMR has no scheduler and background work would ride on UI traffic, so
  precomputing *inside* OpenEMR would make one physician's page load pay for warming everyone's cache).
  The agent is a long-running process, so a nightly job there is ordinary; it reads through the same
  module endpoint under the same authorization. Same conclusion as ARCHITECTURE.md §13 ("if pre-visit
  precomputation is wanted, an external scheduler") and AUDIT.md remediation item 18.
- Request queue with backpressure in front of the provider: 429 + `Retry-After` to the panel, which keeps
  showing the deterministic sections — degraded, never blank.
- Cheaper extraction model if the eval gates hold (Sonnet or Haiku column above); Opus stays for turns if
  their verification-withhold rate is the differentiator.
- OpenEMR: this is where a single instance stops — per-facility or per-health-system OpenEMR deployments
  (tenancy by deployment, which is how OpenEMR is operated anyway), each with its own agent pool and
  shared secret; the agent is multi-tenant by having no state beyond the bundle TTL.
- Observability: sample spans (keep every score and every `degraded`/`hallucinated_span`), per-tenant
  Langfuse projects; alerts per tenant.
- Provider: enterprise agreement with the BAA, rate-limit tier and, if needed, provisioned throughput.
- Infra ≈ $3–6K/mo; model $21K on Opus live, ~$8K with overnight precompute on Sonnet;
  **~$1–2.50 per physician-month**.

**100K physicians — a platform, not a service.** ~1.4 M briefings/day, peak ~150/s; at this size the
question is governance and blast radius, not throughput:
- Regional agent clusters co-located with each health system's OpenEMR; no cross-region PHI movement.
- Provisioned model throughput per region; precompute is mandatory (live extraction at this volume would
  be $209K/mo on Opus and rate-limited); live model calls only for turns and for same-day notes.
- Per-tenant budgets and cost alerts (Langfuse cost by tenant); a kill switch per tenant (the module global
  already disables the agent path without a deploy).
- Change management: model or prompt changes roll out per tenant behind the eval gate and the
  `hallucinated_span` alert; rollback is a config change.
- On-call rotation, SLOs (p95 briefing ≤ 8 s, degraded ≤ 5 %) and the retention/breach-notification
  policies that AUDIT.md COMP-001/005/007 list as open.
- Infra ≈ $30–60K/mo; model $40–60K with precompute on Haiku/Sonnet; **~$0.70–1.20 per physician-month**.

## 6. What drives the number, in order

1. **Which model extracts** — ×1 / ×0.6 / ×0.2. Gated by the eval harness, changed by configuration.
2. **When extraction runs** — live at visit time, or precomputed overnight at batch price. Changes the
   architecture (a scheduler and an extraction cache), and removes the model from the 90-second window.
3. **Follow-up turn rate** — the only open-ended model use; bounded by three tool iterations per turn and the
   immediate refusal for out-of-scope questions (no tool search, ~2.6 s, one step).
4. **Prompt caching** — already on; the stable system prompt is the cached prefix.
5. **Infrastructure** — steps at replica boundaries; never the dominant term below 10K.

## 7. Not included

Anthropic enterprise/BAA pricing (list price assumed); Railway egress; Langfuse Cloud (self-hosted assumed
throughout); engineering and clinical-validation time; the cost of a wrong answer, which the verification
design treats as unbounded and therefore designs out rather than prices.
