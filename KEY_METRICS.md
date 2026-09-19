# Clinical Co-Pilot — Key Metrics

## 1. Product promise

A scheduled outpatient primary-care physician, in the ~90 seconds before an established-patient visit, sees which commitments in the last plan have evidence in the record and which do not — and nothing on the panel is invented ([USERS.md](USERS.md)). Concretely, per briefing: deterministic sections built by the PHP module with no model involved (identity, both reasons, interval results and medication changes, allergies as recorded, a data-quality footer naming every source that could not be read); a plan check from the agent (each lab/test or medication commitment quoted verbatim with a matcher-assigned evidence state and record citations); and scoped follow-up answers whose every statement cites record ids a tool returned in that turn. Two rules govern the metrics as they govern the code: every clinical claim traces to a record the code holds, and a missing record is "no matching record found in the sources searched", never "not done".

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

**Reading.** Online, the rate reduces today to the agent-side proxy `briefings_completed / briefings_started` with the `briefing` p95 read alongside; the full definition needs the module counters listed in §11. The "no unsupported claims" and "correctly patient-bound" conditions are quality properties verified offline and by invariant tests; they are conditions of the definition, not terms that vary on a dashboard. Plan-check *accuracy* (did the extractor find the right commitments, did the matcher assign the right states) is deliberately **not** in the rate: production instrumentation cannot know what the extractor missed. It is an offline release gate (§4).

**Target.** ≥ 95 % — *proposed*, derived from the ≤ 5 % degraded budget in ARCHITECTURE.md §14; no requirement document states a success rate. **Current value: not established** (no live-traffic measurement; the stub-model load baseline gave 100 % at 10 and 50 users, which demonstrates transport behaviour only).

## 4. Offline quality gates (release gates)

Run in CI and on every model or prompt change. A build that fails a gate is not counted in §3 at all.

| Gate | Definition | Threshold | Source of threshold | Action when missed |
|---|---|---|---|---|
| Checked-commitment recall | Labelled `lab_test` and `medication` commitments the extractor produced (verbatim span, correct kind) ÷ all labelled ones. `other` kinds are extracted but not checked, so not scored; no `critical` label exists in the fixtures | ≥ 0.85 | `tests/test_eval_fixtures.py` (live tier assertion) | Inspect missed commitment phrasings by kind; add labelled cases for them; adjust the extraction prompt only with the case in place |
| Extraction precision | Extracted commitments that a labeller also marked ÷ all extracted | ≥ 0.90 | same | Inspect extra commitments: inferred from assessment text vs stated actions; tighten the prompt rules for that pattern |
| Evidence-state precision | Commitments whose assigned state equals the labelled state ÷ commitments with a labelled state | ≥ 0.90 | same; USERS.md §7 | Inspect synonym/LOINC aliasing (`app/synonyms.py`), panel-member handling, corrected/preliminary and conflicting-record rules in `app/matcher.py` |
| Citation completeness | Citations the label requires that were produced ÷ citations required | 1.00 | same | A miss is a matcher or contract bug; fix before release |

The fixture tier fixes `model_output` in each case, so it exercises grounding, matching and citation — not extraction. Extraction P/R comes only from the opt-in live tier, which spends money and was not run for this revision.

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

Not measurable in this project (synthetic data only; no physicians). They are what §3 exists to deliver; without them the technical north star is unproven as physician value. No current values or targets are claimed beyond the one USERS.md already states.

| Metric | Definition | Depends on |
|---|---|---|
| Median physician preparation time | Time from opening the briefing until the physician reports being ready to enter the room | A usability study with a fixed protocol; self-report alone is unreliable at 90-second scale |
| Manual chart-reopen rate | Visits where the physician opens additional chart sections (lab data, prescriptions) to find information the briefing was expected to provide, ÷ eligible visits using the briefing (formula below) | OpenEMR access-log counts, no clinical content (USERS.md §7 "workflow shift") |
| "Would have missed this" rate | Briefing items the physician marks as something they would not otherwise have noticed before entering the room; USERS.md §7 target ≥ 1 per 10 established-patient visits | A one-tap control on the panel recording a count and `cid`, never text; physician feedback, so subject to recall and selection bias |

```text
Visits where the physician opens additional chart sections
to find information the briefing was expected to provide
-----------------------------------------------------------
Eligible visits using the briefing
```

## 9. Current measurements

| Metric | Value | How to read it |
|---|---|---|
| Hallucinated spans / citation completeness | 0 / 1.00 over 24 fixture cases (`eval.py`, 2026-09-18) | Synthetic evaluation: an offline regression baseline, not real-world clinical accuracy |
| Evidence-state precision | 1.00 over 24 fixture cases (2026-09-18) | Same; the cases are self-authored |
| Extraction precision / recall | Last live run 1.00 / 1.00 over 6 plans (2026-09-17; printed, no report committed) | Limited sample — encouraging, not sufficient to establish general extraction performance |
| Isolation and invariant tests | 131 / 131 pass (`uv run pytest`, 2026-09-18) | Tested control behaviour; not zero production incidents |
| Briefing delivery under load | 100 % complete, 0 errors at 10 and 50 users; `briefing` p95 4 ms (`loadtest/BASELINE.md`, 2026-09-17) | Stub provider: infrastructure behaviour, not live-model production performance |
| Live briefing latency | 4.4–5.0 s end to end on `claude-opus-5`, single briefings (`BASELINE.md`) | Observed range from a few runs, not an established p95 |
| Live follow-up latency | ~18.7 s for one turn with two tool calls (`BASELINE.md`) | Single observation; outside the 4 s target; planned fix is a cheaper `MODEL_ID_TURN` measured before switching |
| Verified Briefing Success Rate, follow-up success rate, degraded rate (live) | Not established | No live-traffic measurement recorded |
| Cost per briefing | ≈ $0.03 (≈ 4 K mostly-cached input + 0.5 K output; `copilot-agent/README.md`) | Price-table estimate, not billed spend |

## 10. Reproduction commands

```
cd copilot-agent
uv run pytest -q                                            # isolation + invariant tests (131)
uv run python -c "from app.eval import *; c=load_cases(); r=[run_case(x) for x in c]; s=summarize(c,r); print(len(c), s.state_precision, s.citation_completeness, s.hallucinated_spans)"   # §4 gates (offline, free)
RUN_ANTHROPIC_INTEGRATION_TEST=1 uv run pytest -k live -s   # extraction P/R (spends ~$0.03 per plan; not run for this revision)
uv run python loadtest/run_load.py --base-url <agent> --secret <secret> --users 10 50 --iterations 5   # delivery + latency under stub
curl -s <agent>/metrics                                     # counters, latency percentiles, tokens, estimated_cost_usd (JSON snapshot, not a dashboard)
grep '"event": "span.turn"' <agent stderr log> | jq '{outcome, statements, rejected, tool_calls, duration_ms}'   # follow-up success, withhold per turn
```

## 11. Limitations and next instrumentation steps

- **`/metrics` is a JSON snapshot**, not a dashboard. Self-hosted Langfuse (ARCHITECTURE.md §14) now receives one masked trace per `cid` from the deployed agent with per-generation tokens and cost, per-tool spans and the verification scores, so degraded rate, latency by stage, tool calls and failures, retries (`extract` generations with `attempt > 1`), withhold counts and cost per briefing are queryable there; the dashboard that renders them (`copilot-agent/OBSERVABILITY_GUIDE.md` Part E) is not yet built.
- **The module and panel have no timing and no counters.** Physician-side time to first content, and a full denominator for §3 (refused, `no_prior_note`, `agent_unavailable`, `source_unavailable`), need module counters `tickets_requested`, `tickets_issued{agent}`, `tickets_refused{code}` and a `duration_ms` on the "ticket issued" log line, plus a panel timing beacon.
- **Turn outcomes, tool calls and provider retries are log attributes, not counters.** `turns_started`, `turns_completed{empty}`, `turns_degraded{reason}`, `tool_calls{tool}`, `tool_errors{tool,error}`, `provider_retries{stage}` would make §7 and the retry column dashboard-ready.
- **Ticket rejections are not logged with their reason code**; adding a `log_event("ticket.rejected", code=…)` in `_error` callers and a `ticket_rejected{code}` counter makes patient-binding rejections investigable by reason.
- **`verification_rejected` counts briefings, not proposals**; counting proposals gives the withhold *rate* its denominator.
- **Evaluation data is synthetic and self-authored.** Clinician-authored or de-identified real-style notes are required before any gate in §4 is read as clinical accuracy.
