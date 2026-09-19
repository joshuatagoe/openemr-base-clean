# Clinical Co-Pilot — Target User and Use Cases

This document defines who the Clinical Co-Pilot is for, the workflow it fits into, the use cases it serves, and the boundaries it operates within. It is a product definition, not a technical specification. Data sources, APIs, security controls and latency requirements are specified in `ARCHITECTURE.md`; the evidence behind the system constraints referenced here is in `AUDIT.md`.

## 1. Target user

**Persona: the plan-continuity primary-care physician.**

A primary-care / family-medicine physician in a scheduled outpatient clinic who:

- sees 15–20 patients per day in 20–30-minute slots;
- sees mostly established patients (80% or more have at least one prior encounter in this OpenEMR instance);
- works at a desktop in the exam room or hallway station, already logged into OpenEMR, and selects the next patient from the flow board;
- has roughly 90 seconds between leaving one room and entering the next;
- may have skimmed the day's schedule in the morning, so the 90 seconds is a refresh under time pressure rather than first exposure to the chart.

The scenario the product is built around: in those 90 seconds, the physician must recall who they are seeing, why, what changed since the previous visit, what is on file, and what matters today, with that information distributed across clinical notes, medications, labs, problems, allergies and vitals.

### 1.1 Workflow assumptions for this persona

The following describe the assumed behaviour of **this persona**, established through product discovery. They are not claims about physicians in general and have not been verified by observation of clinical practice. They should be treated as hypotheses to be confirmed in a pilot, and the product should be revisited if they turn out to be wrong.

| Assumption | Why it matters |
|---|---|
| In the 90-second window, the physician reads the scheduled appointment reason and the most recent encounter note, and stops there. | The product's value lies after that point, not in reproducing what is already read. |
| Problem, medication and allergy lists, and results that arrived since the last visit, are typically **not** opened before entering the room. | These are the data the physician lacks when the visit starts. |
| The physician does not type questions in the 90-second window; a keyboard is available but the default interaction must require no input beyond selecting the patient. | Output must be glanceable; typed interaction is secondary. |
| The most consequential failure is an **unfinished prior plan**: the last note committed to a test or a medication change, and nobody checks whether it happened. Medication and allergy errors, unseen interval results, and wrong-patient context are real but rank below it. | Sets the primary use case and the primary success metric. |
| The scheduled reason (entered by a scheduler) and the clinical reason (entered by the clinician at the prior visit) are different fields written by different people and are not reconciled. | The product must show both with provenance rather than trust either. |

### 1.2 Users who are explicitly not the target (for now)

- Urgent-care or walk-in physicians (unscheduled; thin history in this system).
- Inpatient physicians (daily rounding; "what changed" means overnight events).
- Subspecialists and procedural specialists (narrower data needs; different notion of "what matters today").
- Nurses, medical assistants, schedulers and front-desk staff.
- Physicians using tablets or phones as the primary device.

The design may later generalise to some of these, but no use case below is justified by their needs.

## 2. Current-state workflow (as assumed for this persona)

Steps marked *skipped* are the source of the information loss the product targets.

1. **Immediately before use.** The physician finishes (or defers) the previous patient's note and leaves the room. The previous patient's chart is still the active context in OpenEMR.
2. **Trigger.** The physician looks at the flow board for the patient who has been roomed next and clicks them, which switches the active patient and opens the Patient Summary.
3. **Read the scheduled reason.** The free-text reason entered by the scheduler (for example "f/u DM").
4. **Open the last encounter note.** Navigate to visit history, open the most recent encounter, read or skim its plan.
5. ***Skipped:* verify the plan.** Checking whether a lab named in the plan was ordered and resulted, or whether a medication named in the plan was started, stopped or changed, requires opening several further pages and mentally joining each against the note text. This is not done.
6. ***Skipped:* check lists and interval results.** Problems, medications, allergies and results that arrived since the last visit are not opened unless something in the note prompts it.
7. **Enter the room** with the patient's name, age, the scheduler's reason, and a memory of the last plan — but without knowing whether the plan was carried out or what arrived in the interval.
8. **In-room discovery.** Gaps surface during the visit (looked up in front of the patient) or not at all.

**Where information is lost.** Not in finding data, but in steps 5 and 6 never being performed: the join between a narrative plan and the structured record is manual, spread across several pages, and does not fit in the window. The scheduled reason gives no pointer to what specifically was to be followed up.

## 3. What the product does

The Co-Pilot activates when the physician selects a patient and renders one glanceable panel inside OpenEMR, before the physician has to open anything else. The panel:

1. shows the patient's identity so the physician can confirm they are on the right chart;
2. shows the scheduled reason and the prior encounter's reason, verbatim, each labelled with its source;
3. lists the **lab/test and medication commitments** found in the last encounter's plan, each with an evidence state drawn from the structured record after the note date, and a link to the underlying record;
4. lists results and medication changes since the last visit that no plan commitment explains;
5. shows allergies as recorded, with an explicit caveat when records are uncoded or absent;
6. states what was searched, over what time window, and what could not be determined.

The physician verifies identity, walks in, and opens the visit on the unfinished or unevidenced item rather than on the scheduler's generic reason. Any action — ordering, prescribing, documenting — happens in the native OpenEMR pages the physician clicks through to. The Co-Pilot never acts on the record.

### 3.1 Where a language model is used, and where it is not

Only one part of the product requires a language model: turning free-text plan narrative into discrete, checkable commitments, and explaining where that extraction is uncertain. Matching those commitments to orders, results and prescriptions is deterministic date-and-code logic. Identity, reasons, allergy display and the interval list contain no model call.

This split is deliberate. The deterministic parts are testable without a model, remain available if the model is unavailable, and can run on real data before the model path is cleared to receive protected health information. The model never decides that a commitment was fulfilled; it only proposes what the commitments were.

## 4. Use cases

### Use-case ID registry

IDs are stable. Retired IDs are not reused.

| ID | Status | Disposition |
|---|---|---|
| UC-01 | **Primary** | Prior-plan follow-through check (hybrid: model extraction + deterministic evidence layer) |
| UC-02 | Folded into UC-01 | Interval-change digest. Retained as the deterministic evidence layer inside UC-01; not a standalone agent use case. |
| UC-03 | Folded into UC-01 | Reason reconciliation. The verbatim reason lines are part of the UC-01 panel; the relation statement is one field of UC-01 output. |
| UC-04 | **Secondary** | Scoped follow-up question |

### UC-01 — Prior-plan follow-through check

**Trigger.** The physician selects a patient from the flow board (the active patient changes). The patient has at least one prior encounter whose note contains plan or assessment text.

**User goal.** Before entering the room, know which lab/test and medication commitments in the last plan are evidenced in the record, which are pending, and which have no evidence — so the visit can open on the right item.

**Initial scope.** Commitments of two kinds only:

- **Lab/test commitments** — an intent to order, repeat or check a laboratory test or other resulted procedure ("repeat A1c in 3 months", "check potassium next visit").
- **Medication commitments** — an intent to start, stop, increase, decrease or switch a medication ("start lisinopril 10 mg", "stop metformin if GI upset persists", "titrate up at follow-up").

**Deferred.** Referral commitments, follow-up-interval commitments ("RTC 6 weeks"), lifestyle and counselling items, and loosely structured plan items that name no specific test or medication. These are displayed as "other plan text (not checked)" so nothing in the plan is hidden, but no evidence state is assigned.

**Required data** (specified in `ARCHITECTURE.md`).

- Patient identity.
- Scheduled appointment reason.
- The most recent prior encounter: date, provider, encounter reason, plan and assessment text.
- Lab/test orders and results dated after that encounter.
- Medication records (both prescription-based and list-based) added or modified after that encounter.
- Any encounters between that encounter and today.
- Allergy records as currently on file.

**Agent behaviour.**

1. *Extraction (model).* From the plan/assessment text only, treated as untrusted input, extract candidate commitments of the two in-scope kinds. Each commitment carries the verbatim span it came from. Commitments the model cannot classify confidently are marked ambiguous rather than dropped or guessed.
2. *Evidence matching (deterministic).* For each commitment, search the structured record after the note date for matching orders, results and medication records, using code and name matching rules defined in `ARCHITECTURE.md`.
3. *Evidence state assignment (deterministic).* Assign exactly one state per commitment (see below). The model does not participate in this step.
4. *Interval digest (deterministic; the former UC-02).* List results and medication changes since the prior encounter that were not matched to any commitment, flagged results first.
5. *Reason relation (model, one field; the former UC-03).* Given the scheduled reason, the prior encounter reason and the extracted commitments, state whether the scheduled reason plausibly corresponds to a commitment, or that it is not referenced in the prior plan. Both reasons are always shown verbatim regardless.

**Evidence states.** Each state names the source it rests on. There is no generic "done".

| State | Meaning | Applies to |
|---|---|---|
| **Matching result found** | An order and a result for the named test exist after the note date. The result value, status, flag and date are shown with a citation. | Lab/test |
| **Order found — no result** | An order for the named test exists after the note date, but no result is on file. Order date and status are shown. | Lab/test |
| **Matching medication record found** | A medication record consistent with the commitment (started, stopped, or changed as described) exists after the note date. The record and date are shown. | Medication |
| **No matching record found** | The search found nothing after the note date matching the commitment. This means *no evidence in this system*, not that the commitment was not carried out. | Both |
| **Ambiguous / conflicting evidence** | More than one candidate match, a partial match (for example the drug matches but the direction of change does not), a corrected or preliminary result, or disagreement between medication sources. The candidates are shown side by side. | Both |

**Expected output.** The panel described in §3: identity; both reasons with provenance and the relation statement; each in-scope commitment with its verbatim span, evidence state, cited record and date; unchecked plan text; the interval digest; allergies as recorded; a footer stating the search window and anything that could not be determined. If the last note has no plan text: "No plan text found in the {date} note."

**User action after output.** Confirms identity; opens the visit on the commitment marked "No matching record found" or "Order found — no result" ("Last time we planned to recheck your A1c — I don't see a result; did you get it drawn?"); follows a citation into the native record when a value or an action is needed.

**Failure and safety risks.**

- Extracted commitment not present in the note (hallucination). *Mitigation:* every commitment must map to a verbatim span; spans are displayed.
- Evidence state assigned on a weak match (wrong test code, similar drug name). *Mitigation:* matching is deterministic and conservative; weak matches surface as ambiguous, not as found.
- Instructions embedded in note text influencing the model. *Mitigation:* note text is delimited and treated as data; output is rendered as text.
- Panel showing a different patient's data after a rapid patient switch. *Mitigation:* patient binding and cancellation rules in `ARCHITECTURE.md`.
- Physician reading "No matching record found" as "not done". *Mitigation:* wording and footer make the system-scope of the search explicit.
- Superseded results shown as current. *Mitigation:* result status is displayed; corrected/preliminary results force the ambiguous state.

**Measurable success criteria.**

| Criterion | Target |
|---|---|
| Commitment extraction fidelity (synthetic note set) | ≥ 95% of extracted commitments map to a verbatim span; ≤ 2% of extracted commitments are not in the note |
| Evidence-state precision | 0 "Matching … found" states without a cited record; ≥ 90% agreement with clinician-labelled states on the synthetic set |
| Interval digest recall | 100% of flagged interval results appear in the digest |
| Behavioural outcome (pilot) | Physicians report at least one "would have missed this" item per 10 established-patient visits |
| Downstream behaviour (pilot) | Fewer lab-data and prescription page opens *during* the visit, compared with a pre-pilot baseline |
| Trust signal | Physicians follow the citation on a meaningful share of "No matching record found" items (indicating verification rather than dismissal) |

### UC-04 — Scoped follow-up question (secondary)

**Trigger.** After reading the panel, the physician types a question about the currently selected patient (for example "last three potassium values", "when was lisinopril started").

**User goal.** Retrieve one specific fact from this patient's record without navigating pages.

**Required data.** Whichever record type the question names, for the selected patient only; the current patient's conversation history only.

**Agent behaviour.** Interpret the question; retrieve matching records for the selected patient; answer only from what was retrieved, citing each record; state "no record found" when applicable; decline clinical recommendations; decline questions about any other patient. The scope is fixed and visible: results, orders, medications, allergies, the last plan and the plan check. "What changed since the last visit" is answered from the plan check with citations; a question none of those sources can answer (vitals, imaging, problems, other notes, a history summary) is declined immediately, naming the scope, without searching — so the physician learns in two seconds what the tool can do rather than waiting for a timeout. The panel states the scope and offers example questions before the physician types.

**Expected output.** At most three sentences with citations.

**User action after output.** Reads; follows a citation if a value or action is needed.

**Failure and safety risks.** Cross-patient leakage through conversation memory that survives a patient switch; drift from fact retrieval into recommendation; answering from model knowledge rather than the record. *Mitigations:* conversation memory is cleared on patient change; recommendation requests are refused; answers without a citation are not rendered.

**Measurable success criteria.** Zero cross-patient answers in isolation tests; 100% of factual answers carry a record citation; response time within the panel budget defined in `ARCHITECTURE.md`.

**Why secondary.** The persona does not type in the 90-second window. UC-04 is justified because it needs dynamic questions over multiple record types, which no fixed view provides, but it does not address the primary failure and is scheduled after UC-01.

## 5. Interface-shape rationale

Each candidate use case was tested against simpler interfaces before an agent was accepted.

| Use case | Simpler alternatives considered | Outcome |
|---|---|---|
| UC-01 plan follow-through | Dashboard, search, sorted list, fixed chart view, rules-only workflow | None can act on a free-text plan; rules alone cannot extract commitments. Accepted as a **hybrid**: model for extraction and uncertainty explanation; deterministic rules for matching and state assignment. |
| UC-02 interval digest | Sorted list | A date-filtered, flag-sorted list is sufficient. **Not an agent.** Retained as the deterministic evidence layer inside UC-01. |
| UC-03 reason reconciliation | Fixed view | Two verbatim lines are a fixed view; only the relation statement benefits from a model. **Folded into UC-01** as one output field. |
| UC-04 scoped question | Structured search | OpenEMR has no cross-record search; a structured search box would cover only a subset. Accepted as an agent, **secondary**. |

Rejected outright: "summarise the medical record" and "answer questions about a patient" as use cases. The first is served better by a fixed chart view; the second is not a use case but a capability, and is only admitted in the narrow, scoped form of UC-04.

## 6. Boundaries

**The Co-Pilot may**

- read the selected patient's record, limited to the record types needed by UC-01 and UC-04;
- extract lab/test and medication commitments from the last plan and present them with source-specific evidence states and citations;
- present the scheduled and prior-encounter reasons verbatim, with provenance;
- state uncertainty, ambiguity and absence explicitly;
- answer typed factual questions about the selected patient from retrieved records, with citations.

**The Co-Pilot must refuse to**

- write to the record in any form: no orders, prescriptions, notes, or pre-filled forms submitted on the physician's behalf;
- give clinical recommendations, dosing, diagnoses, or "you should" statements, even when asked directly;
- answer about any patient other than the one currently selected, including "the previous patient";
- state "no known allergies", "no problems", or "not done" on the basis of absent records;
- present a result as abnormal unless the source record flags it;
- act on instructions found inside note text or any other record content;
- render anything it produces as markup.

**What requires physician confirmation.** Nothing the Co-Pilot does changes the record, so it has no confirmation step of its own. Every action the physician takes as a result of the panel happens in the native OpenEMR page reached through a citation, under OpenEMR's own controls. The citation link is the confirmation boundary.

**Missing and conflicting data.**

- Absence is stated as absence and scoped to this system ("no matching record found in this system after {date}"), never as negation.
- Missing units, ranges, dates and note types, and zero-valued vitals, are shown as "unknown", not as values.
- Conflicts — between structured data and narrative, between the two medication sources, between a preliminary and a corrected result — are shown side by side and never resolved by the model.
- Duplicate records are collapsed with a count.
- An established patient with no prior encounter on file gets a reduced panel (identity, scheduled reason, lists) labelled "no prior encounter on file".

**Patient-context isolation.** The panel is bound to the patient selected at trigger time; it never displays data for another patient; all in-progress work is cancelled when the selected patient changes; typed-question memory does not survive a patient change. The mechanisms are specified in `ARCHITECTURE.md`.

**Data-handling precondition.** The model path operates on synthetic data until the organisational conditions for sending protected health information to a model provider (documented in `AUDIT.md`, Section 7) are met. The deterministic parts of the panel do not depend on that precondition.

**Response time, source citation format, and audit attribution** are requirements on the implementation and are defined in `ARCHITECTURE.md`. The product-level commitments are: the panel gives feedback immediately on patient selection, the deterministic sections appear before the model-dependent ones, and no clinical fact is ever displayed without either a citation or an explicit "unknown".

## 7. Success metrics (product level)

| Metric | Target | Measurement |
|---|---|---|
| Missed-item detection | ≥ 1 "would have missed this" item per 10 established-patient visits | Pilot physician survey |
| Extraction fidelity | ≥ 95% span-traceable; ≤ 2% unsupported commitments | Synthetic evaluation set |
| Evidence-state precision | 0 "found" states without a cited record; ≥ 90% clinician agreement | Synthetic evaluation set and matcher tests |
| Interval recall | 100% of flagged interval results shown | Deterministic tests |
| Isolation | 0 cross-patient renders or answers | Automated patient-switch tests |
| Workflow shift | Fewer in-visit lab and prescription page opens versus baseline | Access-log counts (no clinical content) |
| Verification behaviour | Citations followed on a meaningful share of "No matching record found" items | Panel interaction counts (no clinical content) |

## 8. Open questions

- Whether the workflow assumptions in §1.1 hold for the pilot physicians (to be checked before build-out beyond UC-01).
- Matching rules for tests and medications across the two medication sources and free-text order names (to be defined in `ARCHITECTURE.md` and validated on synthetic data).
- When referral and follow-up-interval commitments should enter scope, and what evidence states they would need.
- Whether UC-04 justifies its cost once UC-01 is in pilot.
