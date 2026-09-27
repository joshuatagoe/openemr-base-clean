# Patient dashboard parity: PHP vs React port

Evidence for the parity requirement of the dashboard port (`dashboard/`). Each row compares what the PHP patient summary (`interface/patient_file/summary/demographics.php`) shows with what the port shows for the same synthetic patient, on the same dev stack, on the same day.

- **Captured** 2026-09-27 on `docker/development-easy` (OpenEMR 8.2.0-dev, date format `YYYY-MM-DD`). The port screenshots (`*_port.png`, `list_landing_port.png`, `pid7_phone_port.png`) were re-captured after UX wave 3 the same day, with the same recipe (1400 px window, Care Team and Labs expanded; phone at 390 × 844 with the default collapse state).
- **PHP**: signed in as `admin`, the dashboard opened in the patient tab.
- **Port**: mode A (BFF), signed in as `drdash`, a non-admin user in the Physicians group, with the default build (no `VITE_OPENEMR_WEB_URL`, so no edit links).
- **Mode B** (inside OpenEMR, patient-context token): `pid7_modeB_page.png` (from C5, before the UX waves: it still shows the old card chrome and app bar).
- **Patients**: Evelyn Demo (pid 7) and Thomas Reyes (pid 10). All data is synthetic. Pid 7's care team ("C3 Synthetic Team": one active and one inactive member) was seeded for C3.
- **Screenshots**: one element per file, named `pid<N>_<part>_{php|port}.png`.
- **Gap numbers** (G#) refer to the gaps table in `dashboard/README.md` (*FHIR gaps and deliberate differences*).

Status: **match** = same content. Styling follows OpenEMR's default theme (style_light) since the UX waves of 2026-09-27; the remaining visual differences are listed under *Presentation (UX waves 1–3)* below. **gap G#** = a documented difference caused by what OpenEMR's FHIR API exposes. **by design** = a deliberate choice, explained in the README.

## Header (`pid7_header_*`, `pid10_header_*`)

| Field | PHP (pid 7 / pid 10) | Port (pid 7 / pid 10) | Status |
|---|---|---|---|
| Name + (pubpid) | Evelyn Demo (7) / Thomas Reyes (10) | same | match |
| DOB + age | DOB: 1958-04-12 Age: 68 / DOB: 1957-11-30 Age: 68 | same | match |
| Age at death (deceased patient) | `oeFormatAge` | same rule (unit-tested) | match. An age at death under 24 months prints `11 months`, where PHP prints `11months` |
| Sex | not in the bar; the Demographics card shows it as **Birth Sex** (`patient_data.sex`) | `Birth Sex: Female` / `Birth Sex: Male` (FHIR `Patient.gender`, which OpenEMR fills from `patient_data.sex`) | by design: matches the challenge, which asks for sex in the header; worded as OpenEMR's Demographics card. Re-captured 2026-09-27 (`pid*_header_port.png`) |
| Active status | not shown | not shown | match, and intentionally omitted: the challenge lists it, but OpenEMR's FHIR `active` is hard-coded `true` for every patient and the PHP dashboard does not show it |
| Photo, encounter selector, "Open Encounter" | shown | not ported | by design (not part of the summary cards; this app is read-only) |
| Close icon (×) | closes the chart | "Find another patient" | by design (mode A has in-app search) |
| Signed in as / Sign out (modes B/C) | OpenEMR's own top bar | in the patient bar's action slot; no separate app title row, since OpenEMR's tab bar is already above the app | by design (plan L5) |
| Sticky while scrolling | yes (top frame) | yes | match |

## Allergies (`*_allergies_*`)

| Field | PHP | Port | Status |
|---|---|---|---|
| Title | Allergies | Allergies | match |
| Item | Penicillin (Moderate) | Penicillin (Low Risk) | **gap G4**: FHIR has only `criticality`. Mild to moderate maps to "Low Risk"; moderate to severe and worse maps to "High Risk" and is highlighted |
| Tooltip | `Penicillin Reaction:  - Moderate` | `Penicillin Reaction:  - Low Risk` | G4 (same format, criticality in place of severity) |
| Empty text | "No Known Allergies" when reviewed, else "Nothing Recorded" | always "Nothing Recorded" | gap (FHIR cannot say whether the list was reviewed) |
| Order, active only | server order, active | same | match |

## Medical Problems (`*_problems_*`)

| Field | PHP | Port | Status |
|---|---|---|---|
| Items (pid 7, pid 10) | Nothing Recorded | Nothing Recorded | match |
| Title text of a coded problem | `lists.title` | the code's display text | gap G3 |
| Resolved problems | hidden | shown with "(Resolved)" | gap G21 (FHIR `resolved` also covers open "First" episodes) |
| Order | onset ascending | same | match (unit-tested) |

## Medications (`*_medications_*`)

| Field | PHP | Port | Status |
|---|---|---|---|
| pid 7 | Nothing Recorded | Nothing Recorded | match |
| pid 10 | Lisinopril 20 mg | Lisinopril 20 mg | match |
| Split from prescriptions | `lists` vs `prescriptions` tables | standard-API medication list uuids vs FHIR `MedicationRequest` | match in mode A. In modes B/C there is one combined card, because the standard API refuses patient tokens (see `pid7_modeB_page.png`) |

## Prescriptions (`*_prescriptions_*`)

| Field | PHP (pid 7) | Port (pid 7) | Status |
|---|---|---|---|
| Columns | Drug / Details / Qty / Refills / Filled | same | match |
| Drug | Metformin HCl 500 mg | same | match |
| Details | 1 tab BID | same | match here. Form and interval not in FHIR (G7) |
| Qty | 60 | 60 | match |
| Refills | 3 | — (explained by one card footnote: "Refills aren't available from OpenEMR's FHIR API.") | **gap G8**: FHIR always sends `numberOfRepeatsAllowed: 0` |
| Filled | 2026-01-15 09:00:00 | same | match |
| pid 10 (two rows) | Lisinopril 30 / 1 / 2025-08-01; Metformin 30 / 1 / 2025-01-15 | same rows and order, Refills "—" | match except G8 |

## Care Team (`*_careteam_*`)

| Field | PHP (pid 7) | Port mode A (pid 7) | Status |
|---|---|---|---|
| Team name + status badge | C3 Synthetic Team, Active | same | match |
| Member name | Dashboard, Dana | "—", and one note under the team name: "Member and facility names are hidden: your OpenEMR role can't read provider or facility records." | **gap G9**: Physicians get 403 on `Practitioner`/`Organization`. Names resolve in mode B (`pid7_modeB_page.png`: "Dashboard, Dana"). Until 2026-09-27 every such cell said "Name not available (permission)" |
| Role / Facility / Since | Physician / Your Clinic Name Here / 2024-03-01 | Physician / — / 2024-03-01 | match except the facility name (G9, same note) |
| Status / Note | Active / Synthetic note A | — / — (one card footnote: "Status and note aren't available from OpenEMR's FHIR API.") | gap G11/G12 (not in FHIR) |
| Inactive member (admin, 2023-01-15) | hidden | listed | gap G11/G12 (FHIR has no member status) |
| Remove column | shown (edit UI) | not shown | by design (read-only) |
| pid 10, no team | table header only | "Nothing Recorded" | by design (explicit empty state) |
| Default collapsed | yes | yes | match |

## Labs (`*_labs_*`)

| Field | PHP (pid 7) | Port (pid 7) | Status |
|---|---|---|---|
| Heading | Most recent lab data: | same | match |
| Report line | Procedure: Hemoglobin A1c (2026-09-10 08:00:00) | Tests: Hemoglobin A1c (2026-09-12 09:15:00) | **gap G17** (result names, not the procedure name) and **G16** (report date, not collection date) |
| Encounter | 13 (link) | — (card footnote: "The encounter isn't available from OpenEMR's FHIR API.") | gap G17 |
| Link to all lab data | "Click here to view and graph all labdata." | only with `VITE_OPENEMR_WEB_URL` (not set in this build) | gap G24 |
| pid 10 | No lab data documented. | same | match |
| Entered-in-error results | shown | dropped | by design (G18) |
| Default collapsed | yes | yes | match |

## Summary

Across the two patients, the tables above have **42 rows: 23 match and 19 differ**. Of the 19, 13 are FHIR gaps and 6 are design choices (the sixth: sex in the header, added for the challenge). The patient list and recent patients below are not counted: the PHP dashboard has no counterpart. Each one is documented in the README, and no difference is unexplained. (A row counts as "differ" if any part of it differs.) The differences a clinician would notice first:

- the allergy severity wording (G4)
- refills "—" (G8)
- care-team names in mode A (G9)
- the labs line (G16/G17)

## Patient list and recent patients (mode A, beyond the challenge; `list_landing_port.png`)

Added after the parity capture, on the same dev stack as `drdash` (2026-09-27). They mirror OpenEMR's **Patient Finder** (`interface/main/finder/dynamic_finder.php`) and its **recent patients** list; the challenge does not ask for either.

| Feature | OpenEMR | Port | Status |
|---|---|---|---|
| Patient list without a search term | Patient Finder: paged list of all patients | landing page `/dashboard`: 20 per page, Previous / Next, "Page N", page in the URL | mirrors. Live: 14 synthetic patients, one page, sorted Demo … Walsh; `?page=2` says "This page is past the end of the list." with a *Go to page 1* link |
| Sort | Finder's saved column order | last name, then first and middle name (`_sort=family,given`) | mirrors. OpenEMR honours `_count`, `_offset` and `_sort` on Patient searches (checked live with `_count=5`: pages 1 and 2 follow on; `-family,-given` and `birthdate` sort as expected). Ties on the full name have no further tie-break (OpenEMR's sort whitelist has no FHIR key for the uuid) |
| Total / next page | Finder shows "x of N" | no total; "Showing 21–40" and Next enabled only when a 21st row came back | OpenEMR's Bundle has only a `self` link and `total` = the entries returned, so the overall count is unknown |
| Columns | configurable (name, phone, SSN, DOB, …) | Name, DOB, Sex, MRN (`PT` identifier) | by design: never SSN, never phone |
| Search box | Finder's column filters | the existing name / DOB / MRN search narrows the same list, with the same paging | mirrors |
| Recent patients | per user, server-side (`recent_patients`, updated when the PHP dashboard opens a chart, default 20) | per user **in this browser** (hashed `fhirUser` key), **ids only**, max 10, most recent first, updated when a patient opens; names / DOB / MRN read live; gone or forbidden patients dropped silently; *Clear list* button | by design: no REST or FHIR route exposes OpenEMR's list, and the app is read-only. Live: opening Evelyn Demo then Thomas Reyes listed Reyes, Demo; storage held the two uuids only; Clear removed the entry |

## Presentation (UX waves 1–3, 2026-09-27)

Presentation and wording only: no data rule changed (allergy severity mapping, refills "—", the subject guard, entered-in-error filtering and the combined-medications fallback are as above). The plan is `W2_PLANNING/DASHBOARD_UX_PLAN.md`; the look is OpenEMR's default theme (`style_light`) and `demographics.php`, not a redesign.

| Item | PHP page | Port now | Status |
|---|---|---|---|
| Colours and font | style_light: text `#111827`, muted `#4b5563`, links `#1d4ed8`, table head `#e5e7eb`; `"Lato", Helvetica, Arial` (Lato never loaded) | the same tokens and stack (no web font) | match |
| Card chrome | square white card, 1px/1px shadow, bold blue title, `fa-compress`/`fa-expand` and a blue pencil | the same; own solid glyphs in the Font Awesome shapes | match |
| PAMI row | three equal-height cards (`flex-fill`) on a grey page, `container-fluid` | the same | match |
| Primary button, success badge | `#007bff`, `#28a745` | `#0d6efd`, `#198754` | by design: OpenEMR's colours fail WCAG AA with white text |
| Heading focus | n/a | the heading focused after navigation draws no box; controls keep a 3 px ring | by design (a11y) |
| Patient name | link-blue (`ptName` is a link) | dark, same size and weight | by design: here it is not a link |
| Loading | `spinner-border-sm` + "Loading..." | the same spinner (still under reduced motion) + "Loading …"; the patient bar keeps its height while loading | match |
| "—" cells (Refills, Care Team Status / Note, Labs Encounter) | real values | "—" with one muted footnote per card; the column header points to it (`aria-describedby`) | gaps G8, G11/G12, G17; wording only |
| Care-team names hidden by permission (mode A) | names | "—" in the cells, one note under the team name, only when a lookup was refused | gap G9; wording only |
| Card errors | n/a (PHP renders server-side) | per kind: "Your OpenEMR role can't view …", "Couldn't load …: the server couldn't be reached / OpenEMR took too long to answer / OpenEMR returned an error …", with *Try again* for the transient ones | port only |
| Combined medications note (modes B/C) | n/a | "This sign-in can't read OpenEMR's medication list, so medications and prescriptions are shown together, each order once." | wording only |
| Modes B/C app bar | OpenEMR's own bars | no separate title row; *Signed in as* / *Sign out* in the patient bar | by design (plan L5) |
| Phone (390 px) | not responsive | sticky patient bar 65 px, no wrapped button text, cards stacked | port only |
| Page layout (mode A) | patient page `container-fluid` | every page starts on the app title's left edge; the landing page is a 55rem column; the landing and not-found pages share one page title style (1.5rem, weight 500, OpenEMR `.title` size); landing cards sit 0.5rem apart, as on the dashboard | port only (wave 3) |
| Page messages (mode A) | n/a | patient page: "Your OpenEMR account doesn't have access to this patient's chart.", "No patient matches this link. …", "Couldn't load this patient: …" with *Try again*; "Checking your sign-in…"; "Couldn't reach the dashboard server. Check your connection, then reload the page."; a failed sign-in says "Sign-in didn't complete: …" and the button reads *Sign in again*; unknown addresses: "This page doesn't exist." (a page heading) + *Go to the patient list* | wording only (wave 3) |

## Accessibility and layout (port)

- **axe-core in a real browser** (Chrome, all rules including colour contrast):
  - pages checked: patient search with results; patient page with every card expanded; phone width (390 px); the patient-list landing page, with and without the recent-patients list (2026-09-27: 0 violations; at 390 px no horizontal page scroll).
  - result: **0 violations** after two fixes made in C6. The muted text colour was changed from `#6c757d` to `#5c636a` (it measured below 4.5:1 on striped rows and on the page background). The wide table wrappers became focusable regions, so they can be scrolled from the keyboard.
  - UX wave 1 (2026-09-27) set the muted colour to **`#4b5563`** (style_light `$gray-600`, OpenEMR's `.text-muted`; 7.5:1 on white), which also passes on the striped rows and the grey page. Re-run at 1366 px and 390 px: after wave 1 on the landing page and the patient page, and after wave 2 on the patient page with every card expanded (care-team note and footnotes shown), once as loaded and once with the Labs request blocked (error line and *Try again* shown): **0 violations** each time, no horizontal scroll at 390 px. After wave 3 (all rules, built BFF on the dev stack as `drdash`), at 1366 px and 390 px: the landing page empty, with recent patients, with search results and with the partial-DOB error; pid 7 with every card expanded; and the not-found page (its missing `h1` was the one finding, `page-has-heading-one`, fixed in wave 3): **0 violations**, no horizontal scroll at 390 px.
- **PHP page for comparison** (`demographics.php?set_pid=7`, loaded as a top-level page): **34 serious or critical nodes in 7 rules** (`link-name` 17, `color-contrast` 11, `aria-required-parent` 2, `aria-hidden-focus`, `aria-valid-attr-value`, `html-has-lang`, `list`), plus 4 moderate rules.
- **Unit test**: `dashboard/web/test/a11y.test.tsx` runs axe on the signed-out, search, landing (list + pager + recent patients) and patient pages in jsdom. jsdom cannot check colour contrast.
- **Keyboard only**, mode A, every step passed (C6, before the patient list was added; tab counts have changed since). Paging with Previous / Next by keyboard, with focus moving to the new page's table, is covered by `web/test/PatientListFlow.test.tsx`; the dev stack has too few patients for a second page:
  1. Tab reaches the name field; type a name, Enter.
  2. Tab reaches the result link (7 tabs); Enter.
  3. Focus moves to the patient's name heading.
  4. Tab reaches the Allergies toggle; Enter collapses it (`aria-expanded=false`); Space expands it.
  5. Tab reaches the Care Team and Labs toggles, and Enter opens them.
  6. Tab reaches Sign out; Enter signs out.
  7. Focus rings are visible (3 px outline).
- **Phone width** (390 px, emulated): every card is stacked at full width (374 px), with no horizontal page scroll (`scrollWidth` = 390). Wide tables scroll inside their card. See `pid7_phone_port.png`.
