# Observability setup guide — Langfuse for the Co-Pilot agent

What this delivers, mapped to the AgentForge requirements:

| Requirement | How this guide meets it |
|---|---|
| Real-time dashboard: requests, error rate, p50/p95, tool calls, retries, verification pass/fail | Langfuse dashboard built from one trace per briefing (Part E) |
| At least three alerts with documented on-call response | Langfuse Alerts → Slack/webhook (Part F); responses already written in ARCHITECTURE.md §14 |
| `/ready` checks the observability backend | `COPILOT_LANGFUSE_HOST` on the agent (Part C) |
| Correlation id in every log, tool call and LLM interaction | Trace id derived from `cid` (Part B) |
| Token usage and cost per request | `usage_details` / `cost_details` on every generation (Part B) |
| PHI never leaves the control boundary | Self-hosted on Railway + SDK `mask` hook (Parts A, B) |

Time: about half a day the first time. Order matters — do Part A while Part B builds, then C, D, E, F.

---

## Part A — Deploy Langfuse on Railway (service C)

### A1. One-click template

1. Open the Railway template: **https://railway.com/deploy/exma_H** (linked from [langfuse.com/self-hosting/deployment/railway](https://langfuse.com/self-hosting/deployment/railway)). It is community-maintained; Langfuse calls support "best-effort".
2. Deploy it **into the same Railway project** as OpenEMR and the agent, so the services share the private network.
3. Wait for every service to go green. Langfuse v3/v4 needs, and the template provisions: `langfuse-web`, `langfuse-worker`, PostgreSQL, ClickHouse, Redis, and MinIO (S3-compatible blob storage). Confirm you see all six on the canvas.
4. On `langfuse-web` → Settings → Networking → **Generate Domain**. This is the URL you and the agent will use (call it `LANGFUSE_URL`, e.g. `https://langfuse-web-production-xxxx.up.railway.app`).
5. Set `NEXTAUTH_URL` on `langfuse-web` to exactly that URL (the template may default it to something else). Redeploy the web service.
6. Verify: `curl -s $LANGFUSE_URL/api/public/health` → `{"status":"OK", ...}`. This is the endpoint the agent's `/ready` will probe.

### A2. First login and keys

1. Open `LANGFUSE_URL`, create the first user (this becomes the org owner). Keep the credentials in your password manager — this UI will show masked traces only, but it is still a production console.
2. Create an **Organization** → **Project** named `clinical-copilot`.
3. Project → Settings → **API Keys** → Create. Save `pk-lf-…` and `sk-lf-…`; the secret is shown once.
4. Project → Settings → General: note the **Project ID** if you want to link dashboards from docs.

### A3. Move to v4 (needed for Alerts)

The template deploys **v3**. Alert rules with Slack/webhook delivery are a **v4+** feature on self-hosted ([docs](https://langfuse.com/docs/observability/features/alerts)). Because this is a fresh install with no data, the upgrade is small:

1. **ClickHouse first.** On the ClickHouse service, set the image to a version **≥ 25.12** (26.4 recommended) and redeploy. Wait for green. (v4 requires ClickHouse ≥ 25.12, Postgres ≥ 15, Redis ≥ 7.0 — the template's Postgres and Redis already satisfy this; check ClickHouse.)
2. On `langfuse-web` and `langfuse-worker`, change the image to the latest **v4** tag (check [hub.docker.com/r/langfuse/langfuse/tags](https://hub.docker.com/r/langfuse/langfuse/tags) and `langfuse/langfuse-worker`; the major-version tag `4` tracks the latest v4 release).
3. Add to **both** web and worker: `LANGFUSE_MIGRATION_V4_WRITE_MODE=events_only` (fresh install → nothing to migrate; this is the default and the only mode that gives the full v4 UI).
4. Redeploy worker, then web. Verify `/api/public/health` again and that the project you created still opens.

If anything in A3 fights you, stop and use the fallback in Part F — the dashboard works on v3; only native alerts need v4. Record the deviation in ARCHITECTURE.md §14.

### A4. Lock it down

- `langfuse-web` is the only service that needs a public domain. Leave Postgres/ClickHouse/Redis/MinIO on the private network only.
- Settings → Members: no invites beyond you. Organization → Settings: disable public sign-up (`AUTH_DISABLE_SIGNUP=true` on `langfuse-web`).
- Postgres and ClickHouse volumes hold **masked** traces only after Part B, but treat them as sensitive: they are inside the same Railway project as OpenEMR and are covered by the same backup/retention decision (AUDIT COMP-001/COMP-007).

---

## Part B — Wire the agent to Langfuse (implemented 2026-09-19)

All changes are inside `copilot-agent/`; `tests/test_tracing.py` covers them. The design: one Langfuse **trace per `cid`** (trace id *is* the cid), one **observation** per `span()`, one **generation** per model call, one **score** per verification outcome. Nothing clinical is ever passed as `input`/`output`/`metadata`.

### B1. Dependency and settings

`langfuse>=4.15` in `pyproject.toml` (SDK v4 is OpenTelemetry-based and works against a v3 or v4 server).

| Variable | Read by | Purpose |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | `TracingSettings` → SDK | `pk-lf-…` |
| `LANGFUSE_SECRET_KEY` | `TracingSettings` → SDK | `sk-lf-…` |
| `LANGFUSE_BASE_URL` | `TracingSettings` → SDK | `LANGFUSE_URL` (public domain, or the Railway private URL `http://langfuse-web.railway.internal:3000` from the agent) |
| `LANGFUSE_TRACING_ENABLED` | `TracingSettings` and SDK | `false` disables export even with keys set; the test suite forces this |
| `COPILOT_LANGFUSE_HOST` | agent `/ready` | same URL; enables the `langfuse` dependency in the readiness probe |
| `COPILOT_LANGFUSE_CAPTURE_IO` | agent | `false` in production; `true` only for synthetic-data eval runs where full prompts may be recorded |

`TracingSettings` (`app/settings.py`) reads the `LANGFUSE_*` variables from the environment or `.env`; tracing is on only when both keys are present. Keys are never logged.

### B2. Client lifecycle and PHI mask

`app/observability.py`: `configure_tracing(...)` is called from `_lifespan` before `yield` (with `environment=COPILOT_ENVIRONMENT` so traces are tagged `production`/`development`), `shutdown_tracing()` after it. `mask_for_tracing` is the SDK `mask` hook — an allow-list (`TRACE_ALLOWED_KEYS`): dict values under an allowed key pass, every other value and every free string is replaced with `<masked>`; numbers, booleans and `None` pass. Plan text, statements, drug and test names, tool arguments and the patient uuid have no allowed key, so they cannot become attributes. `patient_uuid` is deliberately absent — join to the patient through OpenEMR's audit row by `cid`, never in Langfuse. With `COPILOT_LANGFUSE_CAPTURE_IO=true` the mask is identity (synthetic data only).

### B3. `span()` → observation

`span(name, cid=..., **fields)` is unchanged for callers. With tracing on it also opens a Langfuse observation: when no OpenTelemetry span is current it starts the trace with `trace_context={"trace_id": cid.hex}`; otherwise it nests under the current one. On exit the collected attributes become `metadata`, `outcome in (error, degraded)` sets `level=ERROR`, and `reason_code`/`error_type` becomes `status_message`. The trace name is the root observation's name (`briefing`, `turn`, `briefing.sync`).

### B4. Tool calls as child spans

`app/followup.py` wraps each `run_tool(...)` in `span("tool", tool=name)` and records `records` (count), `truncated` and `error`. Arguments and records are never attached.

### B5. Model calls as generations with usage and cost

`generation(name, **fields)` in `app/observability.py` wraps the two provider awaits: `extract` in `CommitmentExtractor.extract` (one generation per attempt, `attempt` in metadata — a retried extraction shows as two generations) and `turn_step` in `run_turn` (`step` in metadata). The caller sets `gen["usage"] = ModelUsage`; on exit the generation gets `model`, `usage_details` (`input`, `cache_read_input_tokens`, `output`) and `cost_details.total` from `estimate_cost_usd`. `gen["input"]`/`gen["output"]` are forwarded only with capture on. Provider errors are recorded by exception type, never message.

### B6. Verification outcomes as scores

`score(name, value, data_type)` scores the current trace. After a completed briefing: `degraded=0`, `verification_rejected=<count>`, `hallucinated_span=<0|1>`, `state.<evidence_state>=<count>` per state. After a completed turn: `degraded=0`, `verification_rejected`, `hallucinated_span`. Every degraded path (timeout, provider error, internal error, in briefing and turn) scores `degraded=1`.

### B7. Tests

`tests/test_tracing.py`: the mask unit test (plan sentence, drug name and `patient_uuid` are replaced; codes, counts and timings survive); the allow-list contains no clinical or patient key; trace id equals cid. Then, through the real SDK with an in-memory OpenTelemetry exporter: a briefing and a turn produce spans `briefing → extract` and `turn → turn_step, tool` on the one trace whose id is the cid, with no clinical string or patient uuid in any exported attribute, usage/cost on the generation, and input masked; a provider failure is an `ERROR`-level `briefing` observation with `status_message=provider_unavailable` and no exception text. `conftest.py` forces `LANGFUSE_TRACING_ENABLED=false` for every non-live test so a developer's `.env` keys never export test traces. `uv run pytest -q` passes; `-k live` is unchanged.

---

## Part C — Railway variables for the agent

On the **agent** service:

```
LANGFUSE_PUBLIC_KEY       = pk-lf-…
LANGFUSE_SECRET_KEY       = sk-lf-…
LANGFUSE_BASE_URL         = <LANGFUSE_URL>           # or http://langfuse-web.railway.internal:3000
COPILOT_LANGFUSE_HOST     = <LANGFUSE_URL>           # readiness probe; same URL
COPILOT_LANGFUSE_CAPTURE_IO = false
```

Redeploy the agent. `curl <agent>/ready` must now show a fifth dependency, `langfuse: {status: ok}`.

---

## Part D — Verify end to end

1. Open the patient summary on the deployed OpenEMR; let a briefing run; ask one follow-up.
2. Langfuse → Tracing: two traces whose ids match the `X-Correlation-Id` the panel received (visible in the browser's network tab). Open the briefing trace: `briefing` span → `extract` generation with tokens and cost; the turn trace: `turn` → generations and `tool` spans.
3. **PHI check (do not skip):** open every observation's metadata and the generation's input/output. You must see codes, counts, ids and timings only — no plan sentence, no drug or test name, no patient uuid. If anything clinical is visible, the allow-list in B2 is wrong; fix before continuing.
4. Scores tab on the trace: `verification_rejected`, `hallucinated_span`, `degraded`, `state.*` present.
5. Kill the agent's `ANTHROPIC_API_KEY` temporarily (or set `MODEL_PROVIDER=stub` and force an error) and run a briefing: the trace must show `outcome=degraded`, `reason_code`, and the `degraded` score = 1. Restore.

---

## Part E — Dashboard

Langfuse → Dashboards → New. One dashboard, `Clinical Co-Pilot`, with these widgets (each maps to a KEY_METRICS.md row):

| Widget | Source | KEY_METRICS |
|---|---|---|
| **Verified briefing success rate (agent-side)** | `briefing_verified` score average (briefings only) | §3 north star (agent-side proxy) |
| Eligible briefings lost / Ticket outcomes (module side) / Ticket requests | `ticket.*` observations from oe-module-copilot (OTLP, trace id = cid) | §3 denominator; §11 first item |
| Briefings by outcome / Briefings (total) | observation `briefing` count by level / total | §3 numerator and denominator (agent-side) |
| Briefings and turns per hour | observation count, name in (`briefing`, `turn`), grouped by name | context |
| Degraded rate | `degraded` score average (boolean → rate) | degraded briefing rate |
| Degraded by reason | `degraded_reason` categorical score, pie | degraded breakdown |
| Follow-up success rate / Turn outcomes | `turn_success` average; `turn_outcome` categorical pie | §6 follow-up success rate; decision outcomes |
| Verification withhold rate | `hallucinated_span` average (share of traces with a withheld proposal) | verifier-withhold rate |
| Cost per briefing / per turn step | `extract_cost_usd` / `turn_step_cost_usd` averages | §7 cost per briefing |
| Queue depth | not applicable: no queue in the design (bounded concurrency inside one process; a backpressure queue is a 10K-tier change in COST_ANALYSIS.md) | — |
| Error/degraded by reason | observation count grouped by `metadata.reason_code` where level=ERROR | degraded breakdown |
| p50 / p95 / p99 latency | observation latency percentiles, grouped by name (`briefing`, `turn`, `extract`) | latency |
| Tool calls and failures | observation count, name=`tool`, grouped by `metadata.tool`; and by `metadata.error` | tool metrics |
| Retries | generation count, name=`extract`, where `metadata.attempt > 1` | retries |
| Verification withheld | `verification_rejected` score sum and average | verifier-withhold rate |
| Hallucinated spans | `hallucinated_span` score sum (must be 0) | escape guardrail signal |
| Evidence-state distribution | `state.*` scores summed | context |
| Tokens and cost per day | generation usage and cost totals | cost per briefing (divide by briefing count) |

Save, and link the dashboard URL from KEY_METRICS.md §11 and ARCHITECTURE.md §14 (behind Langfuse auth; the link is safe to publish).

---

## Part F — Alerts

Langfuse → Alerts → New Alert (v4). First create an **Automation** (Settings → Automations) targeting Slack or a webhook; then each alert selects it. Definitions and the on-call responses are already in ARCHITECTURE.md §14; put them in the alert's description so the notification carries the runbook.

| Alert | Metric | Threshold | Window | On-call response (§14) |
|---|---|---|---|---|
| 1. Briefing latency | observation `briefing` latency p95 | alert > 8 000 ms (warn > 6 000) | 5 min | Compare OpenEMR vs LLM time in traces; if LLM, lower effort or raise the semaphore |
| 2. Error rate | `degraded` score average, name=`briefing` | alert > 0.05 | 5 min | Check `/ready` of both services, recent deploy, provider status; roll back if deploy-correlated |
| 3. Tool failure rate | observation `tool` count with `metadata.error` set ÷ tool count | alert > 0.10 | 10 min | Inspect the failing source in module logs (service exceptions, ACL); verify DB health |
| 4. Hallucinated span | `hallucinated_span` score sum | alert > 0 | 5 min, renotify every 30 min | Page: disable the agent path via the module global while investigating |
| 5. Agent unreachable from OpenEMR | observation `ticket.agent_unavailable` count | alert > 0 | 5 min | The module built a bundle the agent never received: check the agent's `/health` and `/ready`, Railway deploy state, and `COPILOT_AGENT_URL`; physicians see sections without a plan check meanwhile |

Set "no data" handling to *OK* for 1–3 (quiet clinic hours are not an outage) and to *OK* for 4.

**Single source of truth:** the rules exist only in Langfuse; this table and `copilot-agent/README.md` document them. Self-hosted Slack delivery needs a Slack app (`SLACK_CLIENT_ID`/`SLACK_CLIENT_SECRET`/`SLACK_SIGNING_SECRET` on web) — use a **Webhook** automation with a Slack Incoming Webhook URL instead. Rule 2 uses the `degraded` score average across briefings and turns (the scores view cannot filter by observation name). Rule 3 counts `tool` observations at level `ERROR`, which the agent sets when a tool returns an error code. Recorded in ARCHITECTURE.md §14.

---

## Part G — Documentation updates after it works

- ARCHITECTURE.md §14 **Status** paragraph: Langfuse deployed (service C), tracing wired at `span`, mask allow-list, dashboard and alerts live; §17 Phase 0/5 status cells.
- KEY_METRICS.md §11: remove the "not deployed" bullets; add the dashboard link; note which `TBD` rows are now readable from Langfuse (degraded rate live, follow-up success from `turn` traces, retries).
- `copilot-agent/README.md` Observability section: variables table from Part C; `.env.example` entries.
- `/ready` now checks all three dependencies the brief names — say so in the README's deployed-services table.

---

## Checklist

- [x] Langfuse services green on Railway; `/api/public/health` OK (2026-09-19; redis → `bitnamilegacy/redis:7.2.5`, minio → `quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z`, worker `NEXTAUTH_URL` → `https://${{langfuse-web.RAILWAY_PUBLIC_DOMAIN}}`, S3 credentials → `${{minio.MINIO_ROOT_USER}}`/`${{minio.MINIO_ROOT_PASSWORD}}` on web and worker)
- [x] v4.38 (2026-09-19): ClickHouse `clickhouse/clickhouse-server:26.4`, `langfuse/langfuse-worker:4`, `langfuse/langfuse:4`; on BOTH web and worker: `LANGFUSE_MIGRATION_V4_WRITE_MODE=events_only`, `LANGFUSE_MIGRATION_V4_NATIVE_OTEL_BEHAVIOUR=direct`, `LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN=true` (the image defaults are not the documented ones: without the last two the worker dual-writes OTel to legacy tables and web reads stay on legacy tables, so traces ingest but never show)
- [ ] Sign-up disabled; only `langfuse-web` public
- [x] `langfuse` dependency; `configure_tracing` in lifespan; mask allow-list; mask unit test
- [x] `span()` → observation with trace id = `cid`; tool spans; generations with usage/cost; `attempt` on retries
- [x] Scores: `verification_rejected`, `hallucinated_span`, `degraded`, `state.*`
- [x] Agent env vars set; `/ready` shows `langfuse: ok` (public URL; the private hostname gave `ConnectError` from the agent container — revisit)
- [x] PHI check passed on a real trace from the deployed stack (2026-09-19): briefing + three turns with claude-opus-5, tokens and cost per generation, no clinical string or patient identifier anywhere in the trace
- [x] Dashboard created: [Clinical Co-Pilot](https://langfuse-web-production-818f.up.railway.app/project/cmu8ny4ie0006ok02zd0nib5e/dashboards/cmu8tudy10001ql02y7yb0i7r); widgets per Part E
- [x] Four native v4 alert rules in the UI, runbook text in each description, Slack incoming-webhook automation
- [x] §14, §17, KEY_METRICS §11, agent README updated; commit per milestone
