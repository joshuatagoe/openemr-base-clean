# Patient dashboard parity: PHP vs React port

Evidence for the parity requirement of the dashboard port (`dashboard/`). Each row compares what the PHP patient summary (`interface/patient_file/summary/demographics.php`) shows with what the port shows for the same synthetic patient, on the same dev stack, on the same day.

- **Captured** 2026-09-27 on `docker/development-easy` (OpenEMR 8.2.0-dev, date format `YYYY-MM-DD`). The port screenshots (`*_port.png`, `pid7_phone_port.png`) were re-captured after UX wave 3 the same day (the landing page capture is now `finder_port.png`, see the Patient Finder section), with the same recipe (1400 px window, Care Team and Labs expanded; phone at 390 × 844 with the default collapse state).
- **PHP**: signed in as `admin`, the dashboard opened in the patient tab.
- **Port**: mode A (BFF), signed in as `drdash`, a non-admin user in the Physicians group, with the default build (no `VITE_OPENEMR_WEB_URL`, so no edit links).
- **Mode B** (inside OpenEMR, patient-context token): `pid7_modeB_page.png`, *Patient → Patient Dashboard (React)* for pid 7 as `drdash` (1366 px window, Care Team and Labs expanded). **Mode C**: `pid7_modeC_dialog.png`, the same app launched from the *SMART Enabled Apps* card's *Launch* dialog. Both re-captured 2026-09-27 after the UX waves, with the SMART bundle built from this tree.
- **Signed-out home page** (mode A): `signed_out_port.png` (1366 px) and `signed_out_phone_port.png` (390 px); no patient data.
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

## Patient Finder and recent patients (mode A, beyond the challenge; `finder_php.png` vs `finder_port.png`, phone `finder_phone_port.png`)

The landing page is OpenEMR's **Patient Finder** (`interface/main/finder/dynamic_finder.php`, `dynamic_finder_ajax.php`, `templates/patient_finder/finder.html.twig`), ported for "no loss in functionality" (2026-09-27, branch `dashboard-finder`). Checked live on the dev stack as `drdash` through the BFF (14 synthetic patients) and against the PHP Finder as `admin` at 1366 px. OpenEMR runs the Finder as SQL (`LIKE` per column, `OR` across columns, `COUNT` for the total); the port has only FHIR Patient search, and each row below says where that changes behaviour.

| Finder feature | OpenEMR Finder | Port | Status |
|---|---|---|---|
| Tabs | "Patient List" \| "Recent Patients" | the same (WAI-ARIA tabs, arrow keys; `?tab=recent`) | match |
| Columns | Full Name, Home Phone, SSN, Date of Birth, External ID (`ptlistcols` defaults) | the same; Home Phone = `telecom` phone `use=home` (`phone_home`), SSN = identifier `SS`, External ID = identifier `PT` (`pubpid`) | match. Column set is fixed (no `ptlistcols` configuration, no ColReorder): documented gap |
| "Show [10\|25\|50\|100] entries" | DataTables length menu, default `gbl_pt_list_page_size` (10) | the same, default 10 (`?size=`); BFF `_count` cap raised to 101 (100 + probe row) | match |
| Info / paging | "Showing 1 to 10 of 14 entries", numbered pages | "Showing 1 to 10" and Previous / Page N / Next; "of N entries" only on the last page or when the list is held in the browser | gap: OpenEMR's Bundle has no overall count (only a `self` link, `total` = entries returned), so the port probes one extra row for Next. No numbered page buttons |
| Sort | click any column title; `ORDER BY` that column | Full Name (`family,given` / `-family,-given`), Home Phone (`phone`: phone_home, then phone_biz, phone_cell), Date of Birth (`birthdate`), External ID (`identifier` → pubpid only). All eight checked live: in order | match, except **SSN not sortable** (`ss` is not in `PatientService::ALLOWED_SORT_COLUMNS`, so OpenEMR ignores it): gap. External ID sorts as text (`10` before `9`), as in the Finder |
| Column filters | "Search by Name / Home Phone / SSN / Date of Birth / External ID", filter on keyup, prefix `LIKE` | the same placeholders and labels, applied 400 ms after typing stops or on Enter. Name: `name` (starts with, first/middle/last or title); DOB: `birthdate`, full date (date picker; a partial date is blocked with a message); SSN / External ID: `identifier`, Home Phone: `phone`, refined in the browser so SSN matches only the SS identifier, External ID only the PT one, Home Phone only the home number | gap: SSN, External ID and Home Phone match the **whole value** (FHIR token search is `=`, checked live: a 3-digit phone prefix finds nothing), and DOB needs a full date (the Finder accepts `1977`, `12/1`); labelled in a note under the table |
| Global "Search:" | `OR` over every column, prefix `LIKE` | parallel FHIR searches by the text's shape (letters → name + identifier; digits → identifier + phone (+ birthdate for `1980`, `1980-06`, `1980-06-15`)), merged and de-duplicated by patient id, bounded to the first 100 (the page says so beyond that) | gap, labelled: name prefix, DOB by year/month/day, phone / SSN / External ID whole value only. Checked live: "Demo" → 1 row |
| Search with exact method | check box; `LIKE` without wildcards (case-insensitive) | check box (`?exact=1`); names sent as `name:exact` | gap: OpenEMR's `name:exact` is a case-sensitive `BINARY =` (live: "Demo" 1 row, "demo" 0). Not saved to user settings |
| Open in New Browser Tab | check box, default `gbl_pt_list_new_window` | check box, chart links get `target=_blank rel=noopener`; default off, not remembered | match (default not read from globals) |
| Row click | whole row opens the chart | whole row (stretched link), keyboard on the name link | match |
| Empty states | "No data available in table", "No matching records found", "Showing 0 to 0 of 0 entries" | the same strings | match |
| Add New Patient | heading button (write ACL) | **not ported** | by design: read-only port (read scopes only, no demographics form). Documented in the migration doc |
| Heading icons (collapse, search toggle) | expand/collapse page width; show/hide the filter row | filters always shown; page width fixed | gap (presentation only) |
| Recent Patients tab | server-side `recent_patients` (per user, updated by the PHP chart), columns First Name, Middle Name, Last Name, Date of Birth; "No recent patients" | same columns and empty text; list kept **in this browser**, per user (hashed `fhirUser` key), **ids only**, max 10, names read live, gone/forbidden dropped silently; *Clear list* button | by design: no REST or FHIR route exposes OpenEMR's list. Live: two charts opened → 2 rows; storage held two uuids only |

**PHI handling.** The Finder shows the SSN and home phone, so the port does too (synthetic data only on the dev stack). They appear only in the table. The SSN and Home Phone filters and the Search: text are **never put in the URL** (history, address bar) and never stored: they are kept in memory for the tab and user, so Back from a chart restores them (tests in `PatientListFlow.test.tsx`). Name, DOB and External ID stay in the URL as before. The BFF logs the route pattern only; `logging.test.ts` asserts SSN, phone and External ID values never appear, and the live BFF log (152 lines) contained none of the searched values.

**Accessibility (real Chrome, axe all rules, 2026-09-27):** the Finder list at 1366 px, with a global search, the Recent Patients tab, and the list at 390 px: **0 violations**; at 390 px no horizontal page scroll (`scrollWidth` 390; the five-column table scrolls inside its focusable region).

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
| Signed-out home page (mode A) | n/a (OpenEMR's login page) | page heading "Sign in" (the landing page's title style, focused on arrival), "Use your OpenEMR account to open the patient dashboard.", *Sign in with OpenEMR*; tab title "Sign in – Patient Dashboard" (`signed_out_port.png`, `signed_out_phone_port.png`) | port only (its missing `h1` was an axe `page-has-heading-one` finding) |
| Page layout (mode A) | patient page `container-fluid` | every page starts on the app title's left edge; the landing page is a 55rem column; the landing and not-found pages share one page title style (1.5rem, weight 500, OpenEMR `.title` size); landing cards sit 0.5rem apart, as on the dashboard | port only (wave 3) |
| Page messages (mode A) | n/a | patient page: "Your OpenEMR account doesn't have access to this patient's chart.", "No patient matches this link. …", "Couldn't load this patient: …" with *Try again*; "Checking your sign-in…"; "Couldn't reach the dashboard server. Check your connection, then reload the page."; a failed sign-in says "Sign-in didn't complete: …" and the button reads *Sign in again*; unknown addresses: "This page doesn't exist." (a page heading) + *Go to the patient list* | wording only (wave 3) |

## Accessibility and layout (port)

- **axe-core in a real browser** (Chrome, all rules including colour contrast):
  - pages checked: patient search with results; patient page with every card expanded; phone width (390 px); the patient-list landing page, with and without the recent-patients list (since 2026-09-27 the Patient Finder: see its section) (2026-09-27: 0 violations; at 390 px no horizontal page scroll).
  - result: **0 violations** after two fixes made in C6. The muted text colour was changed from `#6c757d` to `#5c636a` (it measured below 4.5:1 on striped rows and on the page background). The wide table wrappers became focusable regions, so they can be scrolled from the keyboard.
  - UX wave 1 (2026-09-27) set the muted colour to **`#4b5563`** (style_light `$gray-600`, OpenEMR's `.text-muted`; 7.5:1 on white), which also passes on the striped rows and the grey page. Re-run at 1366 px and 390 px: after wave 1 on the landing page and the patient page, and after wave 2 on the patient page with every card expanded (care-team note and footnotes shown), once as loaded and once with the Labs request blocked (error line and *Try again* shown): **0 violations** each time, no horizontal scroll at 390 px. After wave 3 (all rules, built BFF on the dev stack as `drdash`), at 1366 px and 390 px: the landing page empty, with recent patients, with search results and with the partial-DOB error; pid 7 with every card expanded; and the not-found page (its missing `h1` was the one finding, `page-has-heading-one`, fixed in wave 3): **0 violations**, no horizontal scroll at 390 px.
  - Final pass (2026-09-27, all rules): the signed-out home page, which had the same `page-has-heading-one` finding and now has a "Sign in" heading, at 1366 px and 390 px; mode B (pid 7 inside OpenEMR, every card expanded, axe run in the app's frame) at 1366 px and with the tab frame narrowed to 390 px; mode C (the *Launch* dialog) at 1366 px: **0 violations** each, no horizontal scroll at 390 px. OpenEMR's own page around the frame is not counted (it has its own findings, e.g. `frame-title`, `image-alt`).
- **PHP page for comparison** (`demographics.php?set_pid=7`, loaded as a top-level page): **34 serious or critical nodes in 7 rules** (`link-name` 17, `color-contrast` 11, `aria-required-parent` 2, `aria-hidden-focus`, `aria-valid-attr-value`, `html-has-lang`, `list`), plus 4 moderate rules.
- **Unit test**: `dashboard/web/test/a11y.test.tsx` runs axe on the whole document (so page-level rules such as `html-has-lang` and `document-title` run too) for the signed-out, search, landing (list + pager + recent patients) and patient pages in jsdom, and checks each has exactly one `h1`. jsdom cannot check colour contrast, and it always reports `page-has-heading-one` as incomplete, hence the explicit `h1` check.
- **Keyboard only**, mode A, every step passed (C6, before the patient list was added; tab counts have changed since). Paging with Previous / Next by keyboard, with focus moving to the new page's table, is covered by `web/test/PatientListFlow.test.tsx`; the dev stack has too few patients for a second page:
  1. Tab reaches the name field; type a name, Enter.
  2. Tab reaches the result link (7 tabs); Enter.
  3. Focus moves to the patient's name heading.
  4. Tab reaches the Allergies toggle; Enter collapses it (`aria-expanded=false`); Space expands it.
  5. Tab reaches the Care Team and Labs toggles, and Enter opens them.
  6. Tab reaches Sign out; Enter signs out.
  7. Focus rings are visible (3 px outline).
- **Phone width** (390 px, emulated): every card is stacked at full width (374 px), with no horizontal page scroll (`scrollWidth` = 390). Wide tables scroll inside their card. See `pid7_phone_port.png`.
