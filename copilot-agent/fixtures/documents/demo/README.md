# Demo documents

Synthetic documents for the Week 2 demo. Regenerate with
`uv run python fixtures/documents/demo/_generate.py` (deterministic bytes).

They print the seeded demo patient exactly as the chart holds her
(`dev/seed_evelyn_demo.php`: **Demo, Evelyn**, DOB **1958-04-12**, female), so the identity check
passes. Her chart already has HbA1c 8.9 % (2026-09-12) and a lipid panel ordered 2026-09-14 with no
result, which gives the briefing something real to compare against.

Upload each one in OpenEMR under the patient's **Documents** (category *Lab Report*, or *Intake Form*
for intake documents), then open the chart: the panel processes new documents on chart open.

| File | What it shows |
|---|---|
| `demo_lab_followup.pdf` | Text-layer lab report, collected 2026-09-24. HbA1c 8.1 % with **no printed flag**: the briefing states the change from 8.9 % and labels the "above range" comparison as computed, not printed. The pending lipid panel resulted, with the lab's own **H** flags (total cholesterol, LDL, triglycerides), plus fasting glucose H. Boxes appear on each value in the source viewer; values can be verified and filed. |
| `demo_lab_scan.pdf` | Image-only scan (skewed, speckled, no text layer), collected 2026-09-25. Read by AWS Textract; potassium 5.4 mmol/L printed **H**. Shows OCR-verified values with boxes on a scan. |
| `demo_wrong_patient.pdf` | The follow-up report printed for **Demo, Eleanor, DOB 1962-11-03**. Uploaded to Evelyn's chart it is **held**: no values are briefed until a clinician views it and chooses "This is the right patient", or moves it to the right chart. |

Intake-form demo documents live in `../intake/` (see that folder's README).
