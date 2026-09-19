# copilot-agent

The Clinical Co-Pilot agent service (ARCHITECTURE.md §3, §8, §14): Python 3.12,
FastAPI, Pydantic v2, the Anthropic SDK behind a replaceable `ModelProvider`
port. It receives a single-patient `ContextBundle` from the OpenEMR module,
extracts plan commitments with the model, matches them to evidence
deterministically, verifies everything, streams the result to the panel, and
answers scoped follow-up questions. It never holds OpenEMR credentials and
never queries the database.

## Run

```
cp .env.example .env            # fill in ANTHROPIC_API_KEY and COPILOT_TICKET_SECRET
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
uv run pytest                   # 270+ tests, no network
RUN_ANTHROPIC_INTEGRATION_TEST=1 uv run pytest -k live   # opt-in live extraction eval (spends money)
```

`Dockerfile` builds the Railway image; `/health` is the liveness probe and
`/ready` the readiness probe.

## Endpoints

| Route | Caller | Auth | Purpose |
|---|---|---|---|
| `POST /v1/bundles` | module | HMAC (`X-Copilot-Timestamp`, `X-Copilot-Signature`) | Store a bundle for the TTL; returns `bundle_id` |
| `GET /v1/briefings/{bundle_id}` | panel | single-use ticket (`Authorization: Bearer`) | SSE: `commitment`*, `interval_annotation`, then `complete` or `degraded` |
| `POST /v1/conversations/{bundle_id}/turns` | panel | ticket (bound, not consumed) | One verified follow-up turn |
| `DELETE /v1/bundles/{bundle_id}` | panel | ticket (expiry ignored) | Drop bundle and conversation |
| `POST /v1/briefings` | eval, load tests | none | Synchronous briefing over an inline bundle |
| `GET /health`, `GET /ready`, `GET /metrics` | ops | none | Liveness; per-dependency readiness; process metrics |

Every event and response carries `correlation_id` and `patient_uuid`; the panel
drops anything that does not match its own. Tickets bind user + patient +
bundle + cid, expire in 120 s, and are minted only by the module.

## Configuration

Service (`COPILOT_` prefix): `TICKET_SECRET` (≥ 32 chars, shared with the
module), `BUNDLE_TTL_SECONDS` (900), `SIGNATURE_MAX_SKEW_SECONDS` (300),
`BRIEFING_TIMEOUT_SECONDS` (10), `ALLOWED_ORIGIN` (panel origin, CORS),
`OPENEMR_BASE_URL` and `LANGFUSE_HOST` (readiness probes; optional),
`READY_CACHE_SECONDS` (60).

Model: `MODEL_PROVIDER` (`anthropic` | `stub`), `ANTHROPIC_API_KEY`,
`MODEL_ID_EXTRACTION`, `MODEL_ID_TURN`, `EXTRACTION_EFFORT`, `TURN_EFFORT`
(`low`|`medium`|`high`), `EXTRACTION_MAX_OUTPUT_TOKENS`, `TURN_MAX_OUTPUT_TOKENS`,
`PRICE_INPUT_PER_MTOK` / `PRICE_CACHED_PER_MTOK` / `PRICE_OUTPUT_PER_MTOK`
(override the built-in cost table).

`MODEL_PROVIDER=stub` makes no model calls (deterministic extraction from the
curated test table, refusal answers for turns) and is reported as `degraded`
by `/ready` so it cannot be mistaken for production.

## Evidence states

Assigned only by the matcher (`app/matcher.py`, `app/medications.py`):
`matching_result_found`, `order_found_no_result`,
`matching_medication_record_found`, `no_matching_record_found` (no evidence in
this system — never "not done"), `ambiguous_match`, `conflicting_records`,
`verification_unavailable` (a source failed — distinct from "no record").
Commitments of kind `other` carry no state.

## Verification

Extraction: verbatim span containment, per-kind name checks, duplicate
collapse; rejected proposals are counted (`rejected_count`), never rendered.
Follow-up turns (`app/verifier.py`): citations must be record ids that tools
returned in the turn; every number in a statement must appear in a cited
record; `no_record_found` requires an empty search; no recommendation
language; absence is never negation; "high/low/normal" only when the cited
result's flag says so. Known limit: attribution and numeric fidelity, not
semantic faithfulness.

## Observability

JSON logs on stderr, one object per line, keyed by `cid` (`event`, timings,
counts, outcome codes; never clinical values, prompts or completions).
`/metrics` exposes counters (`briefings_started`, `briefings_completed`,
`briefings_degraded{stage,reason}`, `evidence_states{state}`,
`verification_rejected{stage}`), latency percentiles per span (`briefing`,
`briefing.sync`, `turn`), token totals and an estimated cost.

### Tracing (self-hosted Langfuse)

With `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL` set
(`.env` locally; Railway variables in production) the agent exports one trace
per correlation id — the trace id *is* the `cid`, so a panel's
`X-Correlation-Id` finds its trace directly. Shape: `briefing` → `extract`
(generation, one per provider attempt with `attempt` in metadata);
`turn` → `turn_step` (generation) → `tool` (one per tool call: `tool`,
`records`, `truncated`, `error`). Generations carry `usage_details` and
`cost_details`. Scores on the trace: `degraded` (boolean),
`verification_rejected` (count), `hallucinated_span` (boolean), and
`state.<evidence_state>` counts after a completed briefing.

PHI control: the SDK `mask` hook in `app/observability.py` is an allow-list
(`TRACE_ALLOWED_KEYS`): identifiers, counts, codes and timings pass; every
other value — plan text, statements, drug and test names, tool arguments,
the patient uuid — is replaced with `<masked>` inside the agent process
before export. Model inputs and outputs are attached only when
`COPILOT_LANGFUSE_CAPTURE_IO=true`, which is for synthetic-data evaluation
runs and must stay `false` in production. `tests/test_tracing.py` runs a
briefing and a turn through the real SDK with an in-memory exporter and
asserts no clinical string reaches any span.

`COPILOT_LANGFUSE_HOST` (readiness probe) and `LANGFUSE_BASE_URL` (exporter)
are separate settings that carry the same URL. Tracing is off when either
key is absent; the test suite forces `LANGFUSE_TRACING_ENABLED=false` so a
developer's `.env` keys never export test traces.

### Alerts (ARCHITECTURE.md §14) and what to do

The four rules below are native Langfuse v4 alert rules (primary channel);
`ops/langfuse_alerts.py` is the versioned, tested copy and backup channel: `.github/workflows/copilot-alerts.yml` runs it every 5 minutes
against the Langfuse Metrics API v2 (aggregates only), prints one line per
rule, posts breaches to `ALERT_WEBHOOK_URL` (Slack-compatible) when set,
and exits non-zero on a breach so the run fails and notifies. Run it by
hand with the `LANGFUSE_*` variables in the environment:
`uv run python ops/langfuse_alerts.py`. "No data" in a window is OK.
The rules, in the same order as below: briefing p95 > 8 s (warn > 6 s)
over 5 min; `degraded` score average > 5 % over 5 min; `tool` observations
at level `ERROR` > 10 % of tool spans over 10 min; any `hallucinated_span`
score > 0 over 5 min. The on-call response for each is in the script's
`RUNBOOK` and carried in the notification.

1. `briefing` p95 > 8 s over 5 min — compare `model.usage` latency against
   the span: if the model is slow, lower `EXTRACTION_EFFORT` or switch
   `MODEL_ID_EXTRACTION`; if not, inspect the module's service-read timings
   and OpenEMR's audit-row volume.
2. Error rate > 5 % over 5 min — check `/ready` on both services, the last
   deploy, and provider status; `briefings_degraded{reason=provider_*}`
   names the failure class.
3. `verification_unavailable` share > 10 % over 10 min — a source is failing
   in the module (`sources_unavailable` in the bundle footer); check ACLs
   and the database.
4. Any `verification_rejected{stage=extraction}` in production — the model
   is proposing spans that are not in the note; disable the agent path via
   the module global while investigating.

## Cost

Estimated from token usage with a per-million price table (override with
`PRICE_*`). On `claude-opus-5` a briefing is ~4 K input (mostly cached
system prompt) + ~0.5 K output ≈ $0.03; a follow-up turn with two tool calls
is 2–3 model steps. `/metrics` reports the running total.

## Layout

`app/contracts.py` (all wire shapes), `security.py` (HMAC + HS256 ticket),
`store.py` (TTL bundle + conversation store), `main.py` (routes),
`service.py` (briefing stages), `extractor.py`, `synonyms.py`, `matcher.py`,
`medications.py`, `annotations.py`, `tools.py`, `followup.py`, `verifier.py`,
`eval.py` (fixture tier; cases under `fixtures/cases/`), `providers/`
(`base.py` port, `anthropic_provider.py`, `stub_provider.py`, `prompt.py`),
`loadtest/` (runner, k6 script, `BASELINE.md`).
