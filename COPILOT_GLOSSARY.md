# Co-Pilot glossary — every label, tag and code

What each label in the Co-Pilot panel means, plus the codes that appear in traces and logs.
The panel shows the same definitions on hover (`HELP` in
`interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js`); keep the two in step.

- **Week 1 (note briefing):** when a patient is opened, the Co-Pilot reads the plan of the last visit
  note, finds its commitments ("recheck A1c", "continue metformin") and shows whether the chart holds
  evidence for each.
- **Week 2 (document briefing):** *Brief from all read documents* briefs from every lab report
  already read for this patient (their stored readings; nothing is re-read), grounds it in guidelines
  and shows *What changed*, *Needs attention* and *What to consider*. Intake forms read for the
  patient are included as patient-reported lines. *View source* on a document
  citation opens the page and box it came from.

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
| `patient_reported` | Stated by the patient on an intake form, citing the form. An observation, not a finding, and never filed into the chart (ADR-010). |
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
| Reranker | `cohere.rerank-v3-5:0` = Cohere Rerank 3.5 via Amazon Bedrock (production). `fake-lexical-coverage-v1` = local deterministic ranking, not a learned model (CI and local runs). |
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
| `no_extracted_documents` | No lab document has been read yet for this patient (see the document list). |
| `no_document_on_file` | No PDF, PNG or JPEG in the patient's Documents. |
| `document_unavailable` | The newest document could not be read from OpenEMR. |
| `document_too_large` | The newest document is over 10 MB. |
| `agent_unavailable` | The Co-Pilot agent could not be reached. |
| `document_not_decodable` / `document_not_readable` | The file bytes are corrupt, or the model could read nothing from it. |
| `extraction_unavailable` / `answer_model_unavailable` | A model call failed. After an answer-model failure the record lines still show, without guidance. |
| `provider_not_configured` | The agent has no model key configured. |

---

## Week 2 — documents in this chart (list, source viewer, Verify and file)

When the chart opens, the panel reads any new uploaded documents (two per call, repeated until none
remain) and lists every document. Nothing is filed into the chart automatically: each value waits
for a clinician to compare it with its highlighted source and **Verify and file** or **Reject** it.

**Document status** (badge on each document)

| Label | Meaning |
|---|---|
| Waiting to be read | Not read yet. Documents are read when the chart is opened, two at a time. |
| Being read | Being read now. |
| Read | Read. Its values are listed as candidates; nothing is in the chart until a clinician files it. |
| Could not be read | Failed this time; the reason is shown. Most failures are retried the next time the chart opens (up to three attempts). |
| Already read (same file) | The same file was already read in this chart; its values are under that document. |
| Not read / Needs a category | Needs the "Lab Report" or "Intake Form" category, or is a file type the Co-Pilot does not read (only PDF, PNG, JPEG, ≤ 10 MB). |
| Held: identity check | The name or date of birth printed on the document does not match this chart (or the same file is in another chart and nothing printed here confirms this patient). Its values are not shown or used until a clinician confirms the patient with "This is the right patient"; if it is another patient's, move it in Documents. |
| View document → This is the right patient | On a held document, after its explanation: view the document first, then confirm it belongs to this patient (a second click, "Confirm: this is the right patient", sends it). The document becomes Read and its values reviewable — nothing is filed automatically. The identity check result is kept as history, and the confirmation (clinician, time, the hold code and identity result) is an EHR audit row. Needs lab-write and sign permissions, like filing. If the document is another patient's, move it in Documents instead; it is then read again in the right chart. |
| Held for an identity check …; a clinician confirmed this is the right patient | A Read document that was held and then confirmed with "This is the right patient" (code `identity_confirmed_by_clinician`). The original identity result (did not match / could not be compared / same file in another chart) is still shown. |
| N to review | Values from this document waiting for a clinician to file or reject. |
| N patient-reported | On an intake form: the items the patient wrote (chief concern, medications, allergies, family history). Evidence only — not filed, and not counted as values waiting for review. |
| same file in another patient's chart | The identical file is also filed in another chart. With a matching printed name/DOB here, the other copy is the likely misfiling; without one, this copy is held. |

**Value verification** (how the system located the value on the page — never a model's claim)

| Label | Meaning |
|---|---|
| Found on the page | This exact value was found on the page; the box shows where. Found is not the same as correct — check it. |
| Found on the page (close match) | A close match was found (e.g. different spacing); the box shows where. |
| Not found on the page | No box. Shown with "Could not locate this value on the page (unverified)"; filing needs an extra confirmation that you checked it yourself. |
| Unreadable | The value could not be read; it can only be filed with a value you type from the document. |

**Value status and actions**

| Label | Meaning |
|---|---|
| Waiting for review | Read from the document, not in the chart. |
| Review source | Opens the document at the value's page with the box drawn over it, and the filing controls beside it. |
| Verify and file | Files the value as an outside-lab result after you compared it with the source. Filing is signing: needs lab-write and sign permissions. A changed value is filed as corrected (the value as read is kept). A missing collection date must be entered from the document or another reliable record — never guessed, never the upload date. |
| The collection dates differ | The date you entered differs from the one read from the document. Both are shown side by side; filing your date needs a confirmation and a reason, recorded in the EHR audit log. |
| same result already in the chart | Filed, with a warning: a result with the same test, date and value was already in the chart — check for a duplicate. |
| Filed ✓ | Verified and filed into the chart by a clinician. |
| Reject | Marks the value not to be filed (two clicks). It cannot be filed afterwards. |
| Un-file / Un-filed (entered in error) | Withdraws a filed value (two clicks): the chart result is kept for history, marked entered-in-error, and no longer counts as chart data. It cannot be filed again. |
| Source document | On a chart result filed from a document: opens the page and box it came from. |
| Box on the page | Where the system found the value. It shows where it read, not that it read correctly. |

**Intake forms** (documents in the "Intake Form" category; ADR-010)

| Label | Meaning |
|---|---|
| Intake form | A patient intake form (typed, or a photo of a handwritten form). Read like a lab report; the name and date of birth written on it are compared with the chart (identity check) and are not stored. |
| Chief concern / Current medication / Allergy / Family history | One item the patient wrote, as read. "(none reported)" means the patient actually wrote "none" (e.g. "None known"); a blank section is not shown as an item and is never read as "no known allergies". |
| Patient-reported | What the patient wrote on the form: an observation, not a clinical finding, and not a chart record. |
| Patient-reported evidence: shown with its source, not filed into the chart this week | Intake items have **View source** (the form at the item's box) but no **Verify and file** or **Reject**. The chart already has medication and allergy lists; adding the patient's own list beside them without comparing item by item could create duplicates or conflicts. That comparison is a separate step. |
| Not found on the page (intake item) | The system could not find the item on the form, so there is no box. Check the form yourself before relying on it. |
| unreadable (intake item) | The entry could not be read. Nothing was guessed; open the form to read it yourself. |
| [patient-reported] line in the briefing | An intake item in the document briefing, citing the form. Unreadable entries are listed under *Needs attention*. A blank allergy or medication section is stated as a limitation ("not a statement of no known allergies"). Reported medications are not compared with the chart's medication list in the briefing, and the briefing says so. |

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
