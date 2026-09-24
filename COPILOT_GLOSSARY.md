# Co-Pilot glossary — every label, tag and code

What each label in the Co-Pilot panel means, plus the codes that appear in traces and logs.
The panel shows the same definitions on hover (`HELP` in
`interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js`); keep the two in step.

- **Week 1 (note briefing):** when a patient is opened, the Co-Pilot reads the plan of the last visit
  note, finds its commitments ("recheck A1c", "continue metformin") and shows whether the chart holds
  evidence for each.
- **Week 2 (document briefing):** *Brief from latest lab document* reads the newest uploaded lab
  report, grounds it in guidelines and shows *What changed*, *Needs attention* and *What to consider*.

---

## Week 1 — plan check

**Commitment labels** (left of each row)

| Label | Meaning |
|---|---|
| `#1`, `#2` … | Commitment number, in plan order. Other sections refer back to it ("explained by #2"). |
| `lab/test` | The plan said to order or recheck a lab test. |
| `medication` | The plan said to start, stop, change or continue a medication. |
| `other` | Anything else (referral, counselling, follow-up). Listed so nothing is dropped; not checked. |

**Evidence states** (right of each row). Assigned by deterministic matching code, never by the model.

| State | Meaning |
|---|---|
| Matching result found | A result for the test exists after the plan was written. Cited. |
| Order found, no result yet | The test was ordered but no result is filed. |
| Matching medication record found | A prescription or medication-list entry exists. Shows the record exists, not that the patient took it. |
| No matching record found in this system | Nothing matched **in the sources searched**. Never means "not done" — it may be recorded elsewhere. |
| Ambiguous match | More than one record could match; the system does not guess. |
| Conflicting records | Records disagree; both are shown, the conflict is not resolved for you. |
| Verification unavailable | The source could not be checked at all (failed to load). Different from "no record found". |
| Not checked | A commitment kind that is not checked against the record (`other`). |

**Other Week 1 labels**

| Label | Meaning |
|---|---|
| explained by #n | A change in the record since the last visit matches plan commitment #n. |
| unexplained by the plan | A change in the record that matches nothing in the plan — worth a look. |
| flagged H (as recorded) | The abnormal flag exactly as stored in the chart; not computed by the Co-Pilot. |
| active / inactive (field) | Medication record status, and which field says so. |
| table name (e.g. `prescriptions`) | The OpenEMR table the record came from. |
| coded / as recorded (uncoded) | Allergy recorded with a standard code, or as free text shown verbatim. |

## Week 1 — follow-up answers

| Label | Meaning |
|---|---|
| fact | A statement from a record in this chart, with its citation. Uncited statements are removed before display. |
| no record found | Searched and found nothing — scoped to what was searched, not proof of absence. |
| clarification | The question was ambiguous; the Co-Pilot asks rather than guesses. |
| refusal | Outside scope (dosing advice, another patient, an action). One of two fixed sentences, never model wording. |

---

## Week 2 — document briefing

**Assertion tiers** (the grey badge at the start of each line). Every line carries exactly one.

| Tier | Meaning |
|---|---|
| `document_stated` | Printed in the uploaded document, quoted exactly. Not yet confirmed by a clinician. |
| `chart_fact` | Already recorded in the patient's chart. |
| `computed` | Worked out by this system with a fixed rule (e.g. value above the printed reference range). The lab did not print it; the inputs and rule are shown. Never displayed as a lab flag. |
| `patient_reported` | Stated by the patient on an intake form. An observation, not a finding. (Intake forms are not built yet.) |
| `guideline_supported` | What a published guideline says, quoted and attributed, with why it bears on this patient. Describes the guidance; never a recommendation or order. |

**Other Week 2 labels**

| Label | Meaning |
|---|---|
| not yet in the chart | Read from the document but not filed. Nothing is filed without a clinician checking it against the source. |
| flag printed on the report: H | The H/L flag exactly as the lab printed it. |
| computed by this system … / rule: … | Our comparison against the printed range, with its inputs and rule — shown instead of a flag when the lab printed none. |
| Source: document N, p. 1, as printed: "…" | The stored OpenEMR document, page, and the exact text the value was read from. |
| Why this patient | Which of this patient's results the guidance bears on. |
| Uncertainty | What the evidence does not settle for this patient. |
| Guideline: publisher, year · population · evidence Tier | Who wrote the guidance, for whom, and how strongly it may be used (below). |

**Evidence tiers** (on guideline citations)

| Tier | Meaning |
|---|---|
| A | Guidance for general US adults in primary care (NDEP). Can support a consideration. |
| B | Care-process material (CDC). Never the sole support for a threshold or target: such a claim resting only on Tier B is dropped before display. |

**Footer**

| Item | Meaning |
|---|---|
| Route | The supervisor's handoffs in order, each with its reason code (below). |
| Extraction model / Answer model | The models that read the document and proposed considerations. |
| Reranker | `fake-lexical-coverage-v1` = local deterministic ranking, not a learned model. Cohere Rerank via Bedrock is built and off. |
| Corpus | The exact guideline corpus version the evidence came from. |
| Evidence retrieval | `ok`, or a stated reason guideline evidence is missing — never a silent skip. |

**Routing reason codes** (Route line; `supervisor` spans in Langfuse)

| Code | Meaning |
|---|---|
| `document_pending_extraction` | No structured document yet → `intake-extractor` reads it. |
| `evidence_required` | Extracted, no guideline evidence yet → `evidence-retriever`. |
| `evidence_ready` | Evidence found → `answer` proposes considerations; the critic screen drops unsupported ones. |
| `briefing_complete` | Finished normally. |
| `worker_failed` | A worker failed; the run stops and the briefing is marked degraded with a reason. |

**Degraded reasons** (the panel names the reason when a briefing cannot be produced in full)

| Code | Meaning |
|---|---|
| `no_document_on_file` | No PDF, PNG or JPEG in the patient's Documents. |
| `document_unavailable` | The newest document could not be read from OpenEMR. |
| `document_too_large` | The newest document is over 10 MB. |
| `agent_unavailable` | The Co-Pilot agent could not be reached. |
| `document_not_decodable` / `document_not_readable` | The file bytes are corrupt, or the model could read nothing from it. |
| `extraction_unavailable` / `answer_model_unavailable` | A model call failed. After an answer-model failure the record lines still show, without guidance. |
| `provider_not_configured` | The agent has no model key configured. |

---

## In Langfuse

One trace per briefing; the trace id is the request's correlation id. No document text, lab value or
patient identifier is exported — a test fails the build if one is.

| Name | What it is |
|---|---|
| `briefing` → `extract` | Week 1 note briefing; `extract` is the model reading the plan. |
| `turn` → `turn_step` → `tool` | Week 1 follow-up question. |
| `ticket.*` | Week 1 panel requests, from the OpenEMR module. |
| `document_briefing` | Week 2 root. Contains `supervisor` decisions and the workers `intake-extractor` (→ `lab_extract`), `evidence-retriever` (→ `retrieval.hybrid`, `rerank`) and `answer` (→ `answer_considerations`). |

Week 2 scores on the trace: `extraction_results`, `extraction_verified_fraction` (share of values
found verbatim on the page), `extraction_unreadable`, `retrieval_candidates`, `evidence_snippets`,
`evidence_status`, `considerations_shown`, `claims_withheld`, `routing_steps`, `briefing_grounded`
(true when no claim was withheld), `document_briefing_degraded`, and `*_cost_usd` per model call.
