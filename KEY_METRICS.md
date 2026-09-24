# Clinical Co-Pilot — Key Metrics

## 1. Product promise

A scheduled outpatient primary-care physician, in the ~90 seconds before an established-patient visit, sees which commitments in the last plan have evidence in the record and which do not — and nothing on the panel is invented ([USER.md](USER.md)). Concretely, per briefing: deterministic sections built by the PHP module with no model involved (identity, both reasons, interval results and medication changes, allergies as recorded, a data-quality footer naming every source that could not be read); a plan check from the agent (each lab/test or medication commitment quoted verbatim with a matcher-assigned evidence state and record citations); and scoped follow-up answers whose every statement cites record ids a tool returned in that turn. Two rules govern the metrics as they govern the code: every clinical claim traces to a record the code holds, and a missing record is "no matching record found in the sources searched", never "not done".

## 2. Metric hierarchy

| Role | Metrics |
|---|---|
| North star | Verified Briefing Success Rate (§3) |
| Offline quality gates | Checked-commitment recall, extraction precision, evidence-state precision, citation completeness (§4) |
| Safety / security guardrails | Unsupported-claim escape rate, cross-patient disclosure incidents (§5) |
| Leading / actionable | Verifier-withhold rate, degraded briefing rate, latency, patient-binding rejections (§6) |
| Supporting | Follow-up success rate, cost per briefing (§7) |
| Lagging / pilot | Median physician preparation time, manual chart-reopen rate, "would have missed this" rate (§8) |
| Context only | Lifetime request counts, tokens, conversations, commitments extracted, citations (§6, end) |

## 3. North star — Verified Briefing Success Rate

```text
Eligible briefings that complete within the latency target,
remain correctly patient-bound, and contain no unsupported claims
-----------------------------------------------------------------
Total eligible briefing requests
```

| Term | Definition | Measurable online today? |
|---|---|---|
| Eligible briefing request | A `POST /api/copilot/briefing-ticket` that passed authentication, the six ACL checks and the care-relationship rule, and found a baseline plan note. Authorization refusals (401/403/409) and `no_prior_note` (404) are correct outcomes, counted separately, and excluded | Partly. The module writes an `EventAuditLogger` row for authorization denials, `patient_mismatch`, `no_prior_note` and issued tickets; `source_unavailable` and `internal_error` reach the module log only; `patient_not_found` is neither audited nor logged. No counter exists. Denominator today = audit rows with outcome `issued` (+ the two logged failure classes); agent-side proxy = `briefings_started` |
| Complete | The ticket response carried `sections` with `identity`, `reasons`, `allergies`, `footer` present and every interval list either populated or listed in `data_quality.sources_unavailable`; **and** the SSE stream ended with a `complete` frame, not `degraded` or a client abort | Agent half: yes (`briefings_completed`). Module half: from the "ticket issued" log line (`sources_unavailable`, `agent` outcome) |
| Latency target | `complete` frame within 8 s of the ticket request, p95 (ARCHITECTURE.md §13) | Proxy only: the `briefing` latency reservoir on `/metrics` measures extraction + matching + serialization inside the agent. Module read time and network are not measured (§11) |
| Correctly patient-bound | Every frame the panel rendered carried the `patient_uuid` of the ticket it holds; the agent served the stream only after `bundle_id`, `puuid`, `cid` and `sub` in the ticket matched the stored bundle | Enforced by construction in `_bind_ticket_to_bundle` and the panel's drop check; violations would be a security incident (§5), not a rate term. Not separately counted |
| No unsupported claims | No rendered statement lacks a verbatim source span (commitments) or a citation to a record a tool returned (follow-ups) | Not measurable online — nothing can reach the stream without passing the verifier, so the online value is always zero by construction. Detected offline (§5) |

**Reading.** Online, the rate reduces today to the agent-side proxy: the `briefing_verified` trace score (1 when the briefing completed — every rendered commitment then carries a state and citations by construction — 0 when it degraded), shown as the first tile of the Langfuse dashboard ("Verified briefing success rate (agent-side)"), equivalently `briefings_completed / briefings_started` on `/metrics`, with the `briefing` p95 read alongside; the module-side `ticket.*` spans supply the eligible-request denominator (issued + agent_unavailable) and the "Eligible briefings lost" tile counts the requests the agent never saw; the full definition needs the module counters listed in §11. The "no unsupported claims" and "correctly patient-bound" conditions are quality properties verified offline and by invariant tests; they are conditions of the definition, not terms that vary on a dashboard. Plan-check *accuracy* (did the extractor find the right commitments, did the matcher assign the right states) is deliberately **not** in the rate: production instrumentation cannot know what the extractor missed. It is an offline release gate (§4).

**Target.** ≥ 95 % — *proposed*, derived from the ≤ 5 % degraded budget in ARCHITECTURE.md §14; no requirement document states a success rate. **Current value: not established** (no live-traffic measurement; the stub-model load baseline gave 100 % at 10 and 50 users, which demonstrates transport behaviour only).

## 4. Offline quality gates (release gates)

Run in CI and on every model or prompt change. A build that fails a gate is not counted in §3 at all.

> **Corrected 2026-09-23 (Week 2).** When written, no CI existed, so "Run in CI" described an intent rather than a fact. A GitLab `eval-gate` job now runs on every push and merge request ([`EVAL_GATE.md`](EVAL_GATE.md)), but it gates the **five boolean rubric categories over the 24-case golden set** — not every gate listed below, and not on a model- or prompt-change trigger, which does not exist. The gates below that the job does not cover are aspirations, not enforcement.

| Gate | Definition | Threshold | Source of threshold | Action when missed |
|---|---|---|---|---|
| Checked-commitment recall | Labelled `lab_test` and `medication` commitments the extractor produced (verbatim span, correct kind) ÷ all labelled ones. `other` kinds are extracted but not checked, so not scored; no `critical` label exists in the fixtures | ≥ 0.85 | `tests/test_eval_fixtures.py` (live tier assertion) | Inspect missed commitment phrasings by kind; add labelled cases for them; adjust the extraction prompt only with the case in place |
| Extraction precision | Extracted commitments that a labeller also marked ÷ all extracted | ≥ 0.90 | same | Inspect extra commitments: inferred from assessment text vs stated actions; tighten the prompt rules for that pattern |
| Evidence-state precision | Commitments whose assigned state equals the labelled state ÷ commitments with a labelled state | ≥ 0.90 | same; USER.md §7 | Inspect synonym/LOINC aliasing (`app/synonyms.py`), panel-member handling, corrected/preliminary and conflicting-record rules in `app/matcher.py` |
| Citation completeness | Citations the label requires that were produced ÷ citations required | 1.00 | same | A miss is a matcher or contract bug; fix before release |
| Tool-routing accuracy (follow-up) — gate added post-Week 1 / pre-Week 2 (2026-09-21; not part of the Week 1 submission, made before any Week 2 work) | Samples of a labelled follow-up question whose turn called every `must_call` tool, nothing outside `must_call` + `may_call`, and ended in a refusal exactly when expected ÷ all samples (each question asked N times; decision-level boolean rubric, never prose) | ≥ 0.90 | `tests/test_routing_eval.py` (live tier assertion); `fixtures/routing_cases.json` | Read the observed tool sequences in `EVAL.md`; fix the tool description or the prompt's routing rule for that question class, with the case in place |
| Out-of-scope leak rate — gate added post-Week 1 / pre-Week 2 (2026-09-21; not part of the Week 1 submission, made before any Week 2 work) | Out-of-scope samples (vitals, other notes, appointments, another patient) in which any tool was called ÷ out-of-scope samples | 0 | same | A leak is a scope-rule regression in the follow-up prompt; it also costs the turn budget (measured: ~2.6 s refusal vs a 10 s timeout when the model searched) |

The fixture tier fixes `model_output` in each case, so it exercises grounding, matching and citation — not extraction. Extraction P/R and tool-routing accuracy come only from the opt-in live tiers, which spend money (`EVAL.md` records the last run). Routing is measured statistically because which tool the model reaches for is a probabilistic choice made from the tool descriptions and the prompt; the deterministic tests in `tests/test_routing_eval.py` prove the rubric itself with scripted decisions, so a drop in the live number is a model or prompt change, not a scoring bug.

## 5. Safety and security guardrails

### Unsupported-Claim Escape Rate

```text
Unsupported clinical claims displayed to users
-----------------------------------------------
All clinical claims displayed to users
```

Target **0**. An unsupported claim is a rendered commitment whose `source_span` is not verbatim in the plan text, or a rendered follow-up statement citing a record no tool returned, containing a number absent from the cited record, or using recommendation / absence-as-negation / interpretation language the record's flag does not support. **Detection is offline:** every fixture case asserts `hallucinated_spans == 0` and that every citation resolves inside the bundle; adversarial cases (`14_injected_instruction_in_note`, `15_hallucinated_test_name_rejected`) prove known-bad proposals are withheld; the verifier's invariant tests cover each rejection path. In production an escape would mean a verifier defect, so the audit is a code-path audit plus review of reviewed traces once tracing exists (§11) — not a sample of rendered text. `verification_rejected` is **not** evidence about this metric: it counts proposals that were stopped, not claims that got through. **Action:** any escape stops the release, or disables the agent path via the module global, until reviewed. Known limit: the verifier proves attribution and numeric fidelity, not that the words describe the cited record well (ARCHITECTURE.md §9).

### Cross-Patient Disclosure Incidents

Target **0**. Definition: patient information actually displayed in a different patient's context. Controls: the ticket binds `sub`, `puuid`, `bundle_id`, `cid`; the agent refuses on any mismatch; the panel drops any frame whose `patient_uuid` differs from its ticket's; the bundle is deleted on patient switch. Tests in `tests/test_handoff.py` and `tests/test_followup.py` demonstrate that these controls behave as designed under the tested conditions (ticket for bundle A on bundle B → 403; replayed `jti` → 409; deleted bundle → 404). **Passing tests are evidence the controls work as tested, not proof of zero production incidents**; production evidence needs the tracing and review process in §11. **Action:** any incident is a critical security incident — disable the module, preserve logs, review by `cid`.

## 6. Leading / actionable operational metrics

These, together with the request, error, retry, token, cost and readiness measurements on `/metrics` and `/ready`, support operation and diagnosis. None of them independently proves physician value; that is §3 and §8.

Verifier-withhold rate:

```text
Model proposals rejected or withheld
------------------------------------
All model proposals evaluated
```

| Metric | Definition | Target | Measured by (today) | Action |
|---|---|---|---|---|
| Verifier-withhold rate | Model proposals rejected or withheld ÷ proposals evaluated. Rejection means the safeguard worked; the *rate* is diagnostic | No fixed target; watch the trend | Extraction: `verification_rejected{stage=extraction}` on `/metrics` — counts *briefings with ≥ 1 rejection*, not proposals. Turns: `rejected` and `rejection_codes` on each `span.turn` log line | A rising rate → examine rejected proposals by code for model drift, prompt regression, extraction problems or unfamiliar note patterns; pair with recall (§4) because a model that proposes less looks safer here |
| Degraded briefing rate | Eligible briefings ending in `degraded` at any stage ÷ eligible briefings | ≤ 5 % (ARCHITECTURE.md §14 alert 2) | `briefings_degraded{stage,reason}` ÷ `briefings_started` on `/metrics` for extraction/matching stages. Module-side causes (`agent_unavailable`, `source_unavailable`) are in module logs only | Break down by `stage` and `reason` (timeout, provider code, malformed output, matcher error, agent unreachable, source unavailable); check `/ready`, recent deploy, provider status |
| Latency | Plan check complete p95; follow-up turn median | ≤ 8 s p95; ≤ 4 s median (ARCHITECTURE.md §13) | `briefing` and `turn` reservoirs on `/metrics` (p50/p95/p99/max, agent-side only) | Compare model and effort setting, prompt size and cache hits, retry count, tool-call count per turn; module and network time are unmeasured (§11) |
| Patient-binding rejections | Requests the agent refused for `invalid_ticket`, `expired_ticket`, `ticket_reused`, `ticket_bundle_mismatch`, `ticket_patient_mismatch`, `ticket_correlation_mismatch`, `ticket_user_mismatch`, `bundle_not_found`; frames the panel dropped | Expected ≈ 0 in normal use; nonzero needs investigation, not alarm | Coarse only: uvicorn access log status codes (401/403/404/409) on `/v1/*`. The reason codes are returned in the response body but are **not logged or counted**; the panel's `dropped` counter is in-memory only | These are prevented attempts, not disclosures. Investigate by reason: `expired_ticket` after long reads → stale UI; `bundle_not_found` → rapid chart switching; mismatches → integration defect or abuse |

**Context-only measures** (kept for capacity and cost analysis; not success metrics): lifetime `briefings_started`, raw user count, `tokens{input,cached_input,output,requests}`, number of conversations and turns, commitments extracted, citations produced. They describe volume for capacity and cost analysis, not value.

## 7. Supporting metrics

| Metric | Definition | Target | Measured by (today) | Action |
|---|---|---|---|---|
| Follow-up success rate | Turns returning a non-degraded `VerifiedTurn` with ≥ 1 verified statement ÷ turns with a valid ticket and stored bundle. A grounded `no_record_found` statement is a success (the verifier requires an empty tool result behind it, `absence_without_empty_search` otherwise); a turn whose statements were all withheld is a failure | ≥ 90 % — *proposed* | `span.turn` log lines: `outcome`, `statements`, `rejected`, `tool_calls`, `duration_ms`, joined by `cid`. No `/metrics` counter | Empty turns → inspect `rejection_codes`; degraded turns → same breakdown as briefings |
| Cost per briefing | Estimated model spend ÷ completed briefings | ≤ $0.05 — *proposed* | `estimated_cost_usd / briefings_completed` on `/metrics`, from token counts and a per-model price table (`PRICE_*`); an estimate, not billed spend | Inspect uncached input share, output length, retries, model per stage |

## 8. Lagging physician-pilot metrics

Not measurable in this project (synthetic data only; no physicians). They are what §3 exists to deliver; without them the technical north star is unproven as physician value. No current values or targets are claimed beyond the one USER.md already states.

| Metric | Definition | Depends on |
|---|---|---|
| Median physician preparation time | Time from opening the briefing until the physician reports being ready to enter the room | A usability study with a fixed protocol; self-report alone is unreliable at 90-second scale |
| Manual chart-reopen rate | Visits where the physician opens additional chart sections (lab data, prescriptions) to find information the briefing was expected to provide, ÷ eligible visits using the briefing (formula below) | OpenEMR access-log counts, no clinical content (USER.md §7 "workflow shift") |
| "Would have missed this" rate | Briefing items the physician marks as something they would not otherwise have noticed before entering the room; USER.md §7 target ≥ 1 per 10 established-patient visits | A one-tap control on the panel recording a count and `cid`, never text; physician feedback, so subject to recall and selection bias |

```text
Visits where the physician opens additional chart sections
to find information the briefing was expected to provide
-----------------------------------------------------------
Eligible visits using the briefing
```

## 9. Current measurements

| Metric | Value | How to read it |
|---|---|---|
| Hallucinated spans / citation completeness | 0 / 1.00 over 24 fixture cases (`EVAL.md`, 2026-09-20) | Synthetic evaluation: an offline regression baseline, not real-world clinical accuracy |
| Evidence-state precision | 1.00 over 24 fixture cases (2026-09-20) | Same; the cases are self-authored |
| Extraction precision / recall | 1.00 / 1.00 over the 22 live-eligible cases on `claude-opus-5` (`EVAL.md`, 2026-09-20), all 22 also passing the deterministic checks on the live extraction | Small, self-authored sample — encouraging, not sufficient to establish general extraction performance |
| Tool-routing accuracy / out-of-scope leak rate (post-Week 1 / pre-Week 2 (2026-09-21; not part of the Week 1 submission, made before any Week 2 work)) | 1.00 (36/36) / 0.00 (0/12) over 12 labelled questions × 3 samples on `claude-opus-5` (`EVAL.md`, 2026-09-21). The first run scored 0.92: the advice question was refused without tools every time, but the verifier then dropped the refusal for `recommendation_language` because it quoted the request — the physician got an empty answer. Refusals are now rendered as one of two fixed sentences (ARCHITECTURE.md §9) and the rerun is 36/36 | Three samples per question bounds the estimate loosely (one miss in a case = 0.67); raise `ROUTING_EVAL_RUNS` before reading a per-case number as more than a smoke signal |
| Isolation and invariant tests | 224 / 224 pass in the agent suite (`uv run pytest`, 2026-09-20) and 57 / 57 in the module's PHPUnit suite | Tested control behaviour; not zero production incidents |
| Briefing delivery under load | Deployed agent, real model (2026-09-20): 10 users 49/50 complete, p95 6.5 s, 0 errors; 50 users 141/250 complete with the rest explicitly degraded (`provider_busy`), p95 7.3 s, 0 errors; peak 0.2 vCPU / 253 MB (`copilot-agent/loadtest/BASELINE.md`) | The 50-user burst is ~700 briefings/min, the peak of ~8,000 physicians; a 500-bed hospital peaks near 26/min |
| Live briefing latency | p50 2.9 s, p95 6.5 s at 10 concurrent users on the deployed stack (`BASELINE.md`, 2026-09-20); Langfuse `briefing` p50 2.4 s on single requests | Inside the ≤ 8 s p95 target |
| Live follow-up latency | p50 4.6 s, p95 5.3 s over the first deployed turns (Langfuse, 2026-09-20; ~2.6 s for an out-of-scope refusal) | Near the ≤ 4 s median target; a cheaper `MODEL_ID_TURN` remains to be measured |
| Verified Briefing Success Rate, follow-up success rate, degraded rate (live) | Live on the Langfuse dashboard (`briefing_verified`, `turn_success`, `degraded` scores; module-side `ticket.*` spans for the denominator) — demo traffic only so far | No clinic traffic yet; the tiles report what has run |
| Cost per briefing | $0.0042 measured per extraction on the deployed agent; $0.0058 per follow-up turn (`COST_ANALYSIS.md` §3) | Langfuse-computed from token usage at list price, not billed spend |

## 10. Reproduction commands

```
cd copilot-agent
uv run pytest -q                                            # isolation + invariant tests (131)
uv run python -c "from app.eval import *; c=load_cases(); r=[run_case(x) for x in c]; s=summarize(c,r); print(len(c), s.state_precision, s.citation_completeness, s.hallucinated_spans)"   # §4 gates (offline, free)
RUN_ANTHROPIC_INTEGRATION_TEST=1 uv run pytest -k live_extraction -s   # extraction P/R (spends ~$0.03 per plan)
RUN_ANTHROPIC_INTEGRATION_TEST=1 ROUTING_EVAL_RUNS=3 uv run pytest -k live_routing -s   # tool-routing accuracy + leak rate (~36 turns, ~$0.20, ~3 min)
uv run python -m app.eval --report --live --routing 3 --out ../EVAL.md   # regenerate EVAL.md with both live tiers
uv run python loadtest/run_load.py --base-url <agent> --secret <secret> --users 10 50 --iterations 5   # delivery + latency under stub
curl -s <agent>/metrics                                     # counters, latency percentiles, tokens, estimated_cost_usd (JSON snapshot, not a dashboard)
grep '"event": "span.turn"' <agent stderr log> | jq '{outcome, statements, rejected, tool_calls, duration_ms}'   # follow-up success, withhold per turn
```

## 11. Limitations and next instrumentation steps

- **`/metrics` is a JSON snapshot**, not a dashboard. Self-hosted Langfuse (ARCHITECTURE.md §14) now receives one masked trace per `cid` from the deployed agent with per-generation tokens and cost, per-tool spans and the verification scores, so degraded rate, latency by stage, tool calls and failures, retries (`extract` generations with `attempt > 1`), withhold counts and cost per briefing are queryable there; the [Clinical Co-Pilot dashboard](https://langfuse-web-production-818f.up.railway.app/project/cmu8ny4ie0006ok02zd0nib5e/dashboards/cmu8tudy10001ql02y7yb0i7r) (Langfuse login required; widget list in `copilot-agent/OBSERVABILITY_GUIDE.md` Part E) renders them.
- **The module now reports every ticket outcome to Langfuse** (`ticket.issued`, `ticket.agent_unavailable`, `ticket.no_prior_note`, `ticket.refused`, `ticket.source_unavailable`, `ticket.internal_error`, `ticket.ticket_refresh`; one span per request, trace id = cid), so the §3 denominator is readable on the dashboard ("Ticket outcomes (module side)", "Eligible briefings lost (agent unavailable)"). Still missing: physician-side time to first content (a panel timing beacon), a `duration_ms` on the module's "ticket issued" log line, and an external heartbeat — the module's report reaches Langfuse directly (not via the agent), so it counts agent outages, but a Langfuse outage or a full partition of OpenEMR is invisible to any push-based signal and needs an uptime check that alerts on absence.
- **Turn outcomes and retries are now scores and observations in Langfuse** (`turn_success`, `turn_outcome`, `degraded_reason`, `tool` spans at level ERROR, `extract` generations with `attempt = 2`), so §6–§7 read from the dashboard; `/metrics` still exposes only the briefing counters.
- **Ticket rejections are not logged with their reason code**; adding a `log_event("ticket.rejected", code=…)` in `_error` callers and a `ticket_rejected{code}` counter makes patient-binding rejections investigable by reason.
- **`verification_rejected` counts proposals per briefing or turn** (the score is the count) and `hallucinated_span` marks any withhold; the dashboard's withhold rate is the share of requests with a withhold, not proposals withheld ÷ proposals evaluated — the latter needs the proposal count scored alongside.
- **Evaluation data is synthetic and self-authored.** Clinician-authored or de-identified real-style notes are required before any gate in §4 is read as clinical accuracy.

---

## 12. Week 2 — the document briefing

Everything above is the Week 1 metric set, still in force for the Week 1 briefing. This section adds the metrics for the Week 2 promise: **a physician can file a lab report and, in the time it takes to open the chart, see what it says, what needs attention, and what guidance applies — with every value traceable to the printed page and nothing invented.**

### 12.1 North star — Grounded Document Briefing Rate

**Definition.** Share of document briefings in which *every* displayed claim passes all five boolean rubrics: the output is schema-valid, every value is cited to the text it was read from, every value matches the document, nothing unreadable is guessed and no unprinted flag is reported as printed, and no document content reaches a log.

**Why this one.** The failure that would end clinical use is not a slow or incomplete briefing — it is a confident wrong one: a value the model invented, or our own arithmetic presented as the lab's flag. This metric is 1 only when neither happened anywhere in the briefing, so it cannot be improved by being right on average.

**Target:** 1.00 on the golden set, enforced as a floor by the CI gate. **Current:** 1.00 on all 5 document cases, on recorded real-model output (§12.4).

### 12.2 Safety metrics — these are floors, not targets

| Metric | Rule | Why a floor |
|---|---|---|
| **Invented-value rate** | 0 values reported for a result that is unreadable on the page | A guessed value filed as fact is the defect the whole design exists to prevent |
| **Printed-flag fabrication rate** | 0 flags reported as *printed* when the lab printed none | Presenting our comparison as the lab's is the most consequential display error |
| **Uncited-claim rate** | 0 displayed claims without a resolvable citation | Every claim must point back to a source (`§HP-GND`) |
| **Tier-inadmissible claims shown** | 0 threshold claims resting only on Tier B guidance | Patient-education content is not clinical authority (ADR-006) |
| **Document content in logs** | 0 | `§HP-HIPAA`; exact-string test per case |

Four of the five rubric categories sit at a floor of 1.00 for a reason worth stating: the PRD's 5-point regression tolerance cannot catch a single-case regression at this set size (one case of 29 is 3.4 points). The floors are what catch it. See `EVAL_GATE.md`.

### 12.3 Operational metrics — measured, not projected

Three live runs of the full pipeline against the synthetic lab report, `claude-opus-5`, 2026-09-23:

| Step | Median | Range | Share |
|---|---|---|---|
| Extraction — read the PDF | 4.7 s | 4.0 – 5.3 s | 31 % |
| Retrieval + rerank (local) | 0.005 s | — | ~0 % |
| Answer model — propose considerations | **10.7 s** | 10.3 – 11.4 s | **71 %** |
| **End to end** | **15.0 s** | 14.7 – 16.8 s | |

| Tokens | Input | Output |
|---|---|---|
| Extraction call | 1,907 | 334 |
| Answer call | 2,317 | ~980 |

**Cost per document briefing: $0.039** median (range $0.039–$0.055), both calls on `claude-opus-5` at $5 / $25 per MTok.

**Bottleneck.** The answer model, not document reading — about 1,000 output tokens on Opus. The obvious experiments, each measurable against the same golden set: a lower `effort`, a smaller answer model, or fewer considerations per briefing. None is taken until the golden set says quality holds.

**These are n = 3.** Enough to identify the bottleneck and order of magnitude; not enough for a p95. The per-encounter latency and cost are also recorded as spans and scores in Langfuse (`§CR7`), which is where a real distribution will come from.

### 12.4 Eval-gate metrics

| Metric | Current | Source |
|---|---|---|
| Golden cases | 29 — 24 Week 1 note cases, 5 Week 2 document cases | `copilot-agent/fixtures/cases/`, `fixtures/doc_cases/` |
| Rubric pass rate, all five categories | 1.00 | `scripts/eval_gate.py` |
| Document cases on **real recorded model output** | 5 of 5 | `fixtures/recordings/` |
| Test suite (gate stage 1) | 513 passed, 6 skipped | `uv run pytest` |
| Regressions demonstrated blocked | Week 1 hallucination guard (MR !1); Week 2 computed-flag rule; a changed extraction prompt | `EVAL_GATE.md` |

### 12.5 What is not yet measured

- **The reranker's contribution.** Production uses the local lexical reranker; Cohere Rerank via Bedrock is built but deferred to Final. On the demo report the lexical reranker did not surface NDEP Principle 7 on individualised targets — retrieval precision is the metric that would show whether Cohere fixes that, and it is not yet computed.
- **Intake forms.** Not built; no metric.
- **Real-world accuracy.** Every document is synthetic and self-authored. A 1.00 here says the system is internally consistent and does not invent; it does not say it reads real clinic scans well.
