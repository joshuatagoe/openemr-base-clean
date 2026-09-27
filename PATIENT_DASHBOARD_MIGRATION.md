# Patient Dashboard Migration: PHP to React + TypeScript

This document explains and defends the port of OpenEMR's patient summary dashboard (`interface/patient_file/summary/demographics.php`) to a React + TypeScript app that reads OpenEMR only through its OAuth2/OIDC-protected FHIR and REST APIs. It covers:

- what was built and how to try it
- why this framework
- what moving away from PHP gained, with measurements
- what it cost
- the security problems in OpenEMR we found along the way
- what we would do next

Every number here was measured on the dev stack (`docker/development-easy`) on 2026-09-27 and can be reproduced from the files cited. The app, its tests and its operating notes are in `dashboard/` (start with `dashboard/README.md`). The parity evidence is in `docs/dashboard-parity/`.

"Phase B" refers to our live check of OpenEMR's OAuth and FHIR behaviour, run before any code was written. It used real logins, a physician and an admin account, and public and confidential clients. The notes are kept outside the repository, so the results this document relies on are restated here with their OpenEMR source locations.

## 1. What was ported

| Challenge requirement | Where |
|---|---|
| OAuth2 / OpenID Connect login | Mode A: authorization code + PKCE (S256) + client secret, run by the BFF (`dashboard/bff/src/app.ts`). Modes B/C: SMART EHR launch, code + PKCE as a public client (`dashboard/web/src/smart/launch.ts`). The id_token's `iss`, `aud`, `exp` and `nonce` are checked. No password grant, no refresh tokens |
| Patient header | `PatientHeader.tsx` / `fhir/patient.ts`: name, `(pubpid)`, DOB and age (or age at death), following PHP's own age rules, and sex ("Birth Sex: Female", from FHIR `Patient.gender`; see §4 for why active status is left out) |
| Allergies, Problem List, Medications, Prescriptions, Care Team | `components/ClinicalCards.tsx`, one model per card in `web/src/fhir/*.ts`, all from live FHIR (plus one standard-API route for the medication list) |
| One additional section | **Labs**: the PHP "Most recent lab data" card, rebuilt from FHIR `Observation?category=laboratory` (`fhir/lab.ts`) |
| Feature parity | Card titles, order, empty texts, columns, sort orders, collapse defaults and the responsive layout follow the PHP page. The checklist is in `docs/dashboard-parity/PARITY.md` |
| Beyond the challenge (mode A) | **Patient list** as the landing page, like OpenEMR's Patient Finder: 20 per page, sorted by last name, Previous/Next, the search box narrows it (`pages/PatientSearchPage.tsx`). **Recent patients** above it, like the Finder's recent list (`web/src/recent/`) |

Nothing in OpenEMR's PHP core or database was changed. The only server-side OpenEMR code added is packaging inside our own Co-Pilot module:

- a menu entry
- a launch page that redirects to OpenEMR's own SMART launcher
- a start-script line that writes the SMART client id

### One app, three ways to run it

| Mode | How to open it | Login and data path |
|---|---|---|
| **A: standalone** | [`https://dashboard-production-cf2f.up.railway.app/`](https://dashboard-production-cf2f.up.railway.app/) (Railway service built from `dashboard/Dockerfile`); locally `npm run dev` in `dashboard/`, then http://localhost:5173 | The clinician signs in on OpenEMR's login page through the BFF (confidential client, `user/` scopes). They pick a patient from a paged patient list (or narrow it with the search box, or from their recent patients) and switch between them in the app. The token never reaches the browser |
| **B: inside OpenEMR** | [`https://openemr-base-clean-production.up.railway.app/`](https://openemr-base-clean-production.up.railway.app/): open a chart → *Patient → Patient Dashboard (React)* | SMART EHR launch for the open chart. The same React build is served by OpenEMR's Apache from the module's `public/dashboard/`. A public client with `patient/` scopes; the token is held in memory only |
| **C: SMART app** | The patient dashboard's *SMART Enabled Apps* card → *Launch* | Same as B |

Setup (one-time admin steps, the exact commands, and the Railway steps for each mode) is in `dashboard/README.md`: *Register the OAuth client*, *Register the SMART client* and *Deploying on Railway*. The dev stack's test clinician is `drdash`, a non-admin user in the Physicians group.

Mode A is the answer to the challenge. B and C reuse the same build, so a clinician can also open it from the chart they already have open.

## 2. Why React + TypeScript + Vite

We needed four things:

1. a typed model of FHIR R4, because most of the work is mapping FHIR fields onto what the PHP cards show
2. a data layer with loading, empty, error, retry and cancellation states per card
3. strong testing with fake HTTP
4. a static build that OpenEMR's own Apache can serve unchanged (mode B)

**Why React + TypeScript** won on those four:

- **Typed FHIR.** `@types/fhir` gives the full R4 model. The compiler checks every card mapping against it, and `tsc --noEmit` runs before every build, in CI (`.gitlab-ci.yml`, job `dashboard`) and in the image build. For example, we reintroduced two plausible mistakes and ran the type check:
  - Reading `o.effectiveDate` in `fhir/lab.ts` fails: *"TS2551: Property 'effectiveDate' does not exist on type 'Observation'. Did you mean 'effectiveDateTime'?"*
  - Comparing allergy `criticality === 'severe'` fails: *"TS2367: … types '"low" | "high" | "unable-to-assess"' and '"severe"' have no overlap"*. That is exactly the severity/criticality confusion behind gap G4.
- **Data layer.** TanStack Query gives each card its own cache entry, retry policy and cancellation. The data layer turns each query into one of five states (`web/src/data/queryView.ts`), and every card handles all five.
- **Testing.** React Testing Library + MSW let us render the real app against fake FHIR bundles and fake 401/403/5xx answers without a server.
- **Static build.** Vite's production output is plain static files with no inline script, so it runs under a strict `script-src 'self'` CSP in both hosts.

**Alternatives considered:**

- **Vue 3 + TypeScript** would have worked as well. React won on the FHIR/SMART ecosystem and on testing-library maturity, not on capability.
- **Svelte** has a smaller runtime, but less FHIR tooling and fewer people who can maintain it.
- **Angular** is heavier than a six-card read-only page needs.
- **Next.js** was rejected in planning because its value is a server rendering tier, and the challenge rules out adding backend logic.

**The thin server we still needed (mode A).** We planned a browser-only app with no server. Two blockers, both measured in Phase B on this OpenEMR build, ruled that out:

1. **A public (browser) client cannot get clinician `user/` scopes.** Registration answers `400 invalid_client_metadata: "system and user scopes are only allowed for confidential clients"` (`AuthorizationController.php:329`). An already-registered public client is shown the scopes on the consent page, but `/token` silently drops them.
2. **A browser on another origin cannot call the API at all.** Every CORS preflight to `/apis/*` returns **404 "Route not found"**:
   - `RoutesExtensionListener` (priority 40) dispatches `OPTIONS` like any other method and throws (`HttpRestRouteHandler.php:98`) before `CORSListener` (priority 25) can answer.
   - Chrome then blocks every request that carries `Authorization`.

So mode A has a small **backend-for-frontend** (Fastify, about 820 lines in `dashboard/bff/src/`). It holds **no business logic**:

- It runs the OAuth login as a confidential client.
- It keeps the token server-side, keyed by an HttpOnly session cookie.
- It forwards an **allow-list of read-only GETs**: eleven route patterns with validated query parameters (`bff/src/allowlist.ts`). Anything else is 404, and unknown or repeated query parameters are 400.

All FHIR mapping, parity rules and display logic live in the browser app, and the same code runs unchanged in modes B/C with no BFF at all.

## 3. What moving away from PHP gained

### 3.1 The dashboard can only see what the API lets the user see

The PHP page is 2,080 lines (`demographics.php`) with 38 inline SQL call sites. It reads tables directly and relies on page-level ACL checks.

The port has no database access. Every value comes through OpenEMR's API, under the signed-in user's own token and OpenEMR's own ACLs. When OpenEMR says no, the port says so on the card instead of working around it.

An example: Physicians get 403 on `Practitioner` and `Organization`, so mode A shows care-team names as "Name not available (permission)" (gap G9). We did not grant `admin/users` or use a system credential to hide the gap.

### 3.2 Tests

`npm test` runs **431 tests**: 179 for the BFF and 252 for the web app. They run in CI with no OpenEMR, network or secret: the BFF tests use a mock OpenEMR on a loopback port, and the web tests use MSW.

- **BFF**:
  - config validation: it refuses write scopes, `offline_access`, non-https issuers and short session secrets
  - the login: state is single-use, the nonce is checked, PKCE follows the RFC 7636 test vector, and the secret is always sent
  - cookies
  - the allow-list and query validation
  - upstream error mapping: 401 ends the session; OpenEMR's 500 on an inaccessible patient becomes 403; timeouts become 504
  - security headers
  - logs that never contain URLs, tokens or bodies
- **Web**:
  - one model per card, with the PHP rules as test cases (sort orders, the medication/prescription split, the lab report grouping, age formatting including age at death)
  - the subject guard
  - both data sources
  - the SMART launch (origin checks, state expiry, token kept in memory)
  - full list / search → patient → cards flows, paging, and the recent-patients list (including that storage holds patient ids only and works when storage is blocked)
  - axe-core accessibility checks on the rendered pages (`web/test/a11y.test.tsx`)
- **Module (PHPUnit)**: the Co-Pilot module's suite covers the menu entry, the launch page's decisions and the start script.

For comparison, the only patient-card render test we found in OpenEMR covers the appointments card (`tests/Tests/Isolated/Common/Twig/TwigTemplateRenderTest.php`). We found no test of what `demographics.php` shows for the six cards ported here.

### 3.3 A subject guard OpenEMR turned out to need

- Every FHIR result must reference `Patient/<selected id>`, and every standard-API row must carry the selected pid (`web/src/fhir/subject.ts`).
- Anything else is dropped. The console gets one line with the resource type and the count, and no data.

The guard was written because of a measured bug: under a patient-context token, `Observation?patient=<other>&category=laboratory` ignores the `patient` parameter and returns the *context* patient's results (Phase B T4). Without the guard, a lab card could show one patient's result on another patient's page.

### 3.4 Security posture of the page itself

| | PHP dashboard (dev stack, authenticated `curl`) | Port, mode A (BFF) | Port, modes B/C (`.htaccess` in the module folder) |
|---|---|---|---|
| CSP | none | `default-src 'self'; script-src 'self'; … frame-ancestors 'none'; object-src 'none'; base-uri 'none'` | `default-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'self'; …` |
| Framing | not restricted | `X-Frame-Options: DENY` | `SAMEORIGIN` (OpenEMR's own tab and dialog only) |
| `nosniff`, `Referrer-Policy: no-referrer` | no | yes | yes |
| HSTS | yes | yes (when cookies are Secure) | yes (OpenEMR's) |
| Where the access token lives | n/a (PHP session) | **server-side only**; the browser has an HttpOnly, SameSite=Lax, signed `__Host-` cookie | in a JavaScript closure: never in `localStorage` or `sessionStorage`, gone on reload |
| Writes possible | yes (same page edits) | no: read-only scopes, and the BFF proxies GET only | no: read-only scopes |

The BFF also narrows what it passes on. OpenEMR's `/api/patient/:puuid` returns the whole `patient_data` row, SSN included. The BFF answers only `{pid, uuid}`, the one mapping the medication route needs. The patient list is always bounded: a Patient search without a name, DOB or MRN must carry `_count` (at most 50), and only `_count`, `_offset` and a fixed set of `_sort` values are accepted.

The only thing mode A keeps in the browser about patients is the recent-patients list, and it holds **FHIR patient ids only** (never a name, DOB, MRN or other PHI), under a key derived from a SHA-256 hash of the user's `fhirUser`. Names are read live through the BFF each time the list is shown.

### 3.5 Accessibility

We ran axe-core in Chrome with all rules, including colour contrast. Details are in `docs/dashboard-parity/PARITY.md`.

- **Port: 0 violations** on the search page, on the patient page with every card expanded, at phone width, and (added later) on the patient-list landing page with the recent-patients list. The first run found two serious issues:
  - muted grey text below 4.5:1 contrast
  - wide tables that could not be scrolled from the keyboard on a phone

  Both were fixed in this milestone.
- **PHP page** (`demographics.php?set_pid=7`, loaded as a top-level page): **34 serious or critical nodes in 7 rules**, including 17 links with no accessible name, 11 contrast failures and invalid ARIA on the tab list.

Other checks:

- **Keyboard only**, every step works: search, open a patient, collapse and expand a card with Enter or Space, open Care Team and Labs, sign out. Focus moves to the patient's name after each navigation, and focus rings are visible.
- **Phone width (390 px)**: the cards stack at full width, with no horizontal page scroll.

### 3.6 Measured performance

Setup for the page-load numbers:

- Chrome, pid 7, n = 7 runs each; we report the median (p50).
- "Cards ready" means every card has left its loading state (the port), or the page has loaded and all its AJAX fragments are done (PHP).
- Both pages hit the same OpenEMR container.
- Script: a Selenium script. It is not committed because it needs the dev-stack sign-ins, so the method is described here in full.

| Metric | PHP dashboard | Port (mode A) |
|---|---|---|
| Time to cards, empty cache (p50) | 4,099 ms | **3,108 ms** |
| Time to cards, warm cache (p50) | 3,954 ms | **3,047 ms** |
| Requests per load | 16 | 15 (of which 10 are API calls) |
| Bytes decoded per load | 1.13 MB | 0.35 MB |
| Bytes transferred on a repeat visit | 1.13 MB (the dev stack re-sends it) | 12.7 KB (hashed assets are cached; only JSON moves) |

Other numbers:

- **Bundle**: JS 338.9 kB (**105.7 kB gzip**), CSS 4.35 kB (1.42 kB gzip), `index.html` 0.45 kB. React, the router, TanStack Query and all card logic are included.
- **BFF cold start**: from process start to the first `200 /healthz` takes **227 ms** (p50, n = 9, Node 24).
- **Images**:
  - The dashboard adds a **~3 MB** layer to the OpenEMR image. It was 54 MB until this milestone removed the npm cache the build step left behind.
  - The mode A BFF image is 85 MB compressed (Debian slim, BFF production dependencies only).

How to read the numbers:

- The dev stack runs PHP from a Windows bind mount, so absolute times are slow for both pages. Only the comparison is meaningful.
- The port's time is almost all OpenEMR's API: each FHIR call took 0.6–1.1 s here (BFF request log). The BFF's own routes answer in 1–3 ms.
- The critical path is three calls deep: the Patient read, then the card searches, then the Practitioner/Organization reads or the pid → medication-list lookup.
- The cards wait for the Patient read on purpose: a patient the user cannot open gets one clear message and no further requests. That choice costs about one API round trip (§7).

## 4. Tradeoffs we accepted

1. **A new server and a Node toolchain.** Mode A adds a service to deploy, patch and monitor:
   - a Railway service, a Dockerfile, a healthcheck and one more secret set
   - npm dependencies to keep current
   - the OpenEMR image build now also runs `npm ci` for the dashboard

   Sessions are held in the BFF's memory. It must therefore run as **one replica**, and a redeploy signs everyone out. That is acceptable for a read-only tool with 1-hour tokens, but it is not horizontally scalable as built.
2. **Two apps and two logins in mode A.** The clinician signs in to OpenEMR through the BFF, and the consent page shows. The mode A session and an open OpenEMR session are separate (OpenEMR has no token revocation endpoint; sign-out drops the token, which then expires upstream). Modes B/C avoid the second login when the admin turns on OpenEMR's launch-authorization skip for the client.
3. **One-time OAuth admin work per environment.** For each client:
   - set `site_addr_oath` to the public https origin (it must be explicit behind Railway's TLS proxy)
   - register the client (with `curl`; registration from a browser fails the same CORS preflight)
   - enable it in *API Clients*

   The PHP page needs none of this.
4. **Parity limited by what OpenEMR's FHIR exposes.** The full list is the gaps table in `dashboard/README.md` (G2–G24) and the row-by-row checklist in `docs/dashboard-parity/PARITY.md`. On the two compared patients, 23 of 42 rows match. The other 19 are 13 FHIR gaps and 6 design choices (one is sex in the header, added for the challenge), all documented. The ones a clinician notices:
   - **Allergy severity (G4).** FHIR only has `criticality`, so "Moderate" shows as "Low Risk". Moderate-to-severe and worse show as "High Risk" and stay highlighted, erring on the safe side.
   - **Refills (G8).** OpenEMR always sends `numberOfRepeatsAllowed: 0`, so the port shows "—" rather than a wrong 0.
   - **Care-team names in mode A (G9).** Physicians get 403 on `Practitioner`/`Organization`, so names show as "Name not available (permission)". In modes B/C they resolve. Member status and note are not in FHIR at all (G11/G12), so inactive members are listed.
   - **Labs.** We rebuild the report from `Observation`, not `DiagnosticReport` (which needs `admin/super` on this build):
     - the port shows result names ("Tests: …") where PHP shows the procedure name
     - it shows the report date where PHP shows the collection date
     - it shows no encounter number (G16/G17/G22–G24)
   - **Sex is shown, active status is not.** The challenge lists both; the PHP header shows neither. The header now shows sex as "Birth Sex: Female": FHIR `Patient.gender` is filled from `patient_data.sex`, the field OpenEMR's Demographics card labels "Birth Sex". Active status stays out: OpenEMR's FHIR `Patient.active` is hard-coded `true`, so showing it would claim something the data does not know, and the PHP dashboard does not show it either.
   - **"No Known Allergies" is never shown.** FHIR cannot say whether the list was reviewed.
   - **Medications vs prescriptions in modes B/C.** The split needs the standard API's medication list, which refuses patient tokens. Modes B/C therefore show one clearly labelled combined card rather than guess a split from `intent`.
5. **Collapse state and recent patients are per browser, not per server-side user.** PHP saves each card's expanded or collapsed state in the user's server-side settings. The read-only port keeps it in `localStorage`, under the same setting names.

   OpenEMR also keeps a per-user recent-patients list (`recent_patients`, updated when the PHP dashboard opens a chart and shown by the Patient Finder), but no REST or FHIR route exposes it and the port does not write to OpenEMR. The port therefore keeps its own list in the browser: at most 10 patients, most recent first, per signed-in user (the key is a hash of `fhirUser`, so it does not name the user). It stores **ids only**, so a shared or lost machine reveals no names, and a patient the user can no longer open simply disappears from the list. The cost: the list does not follow the user to another browser and is separate from OpenEMR's own list.
6. **No deep links back into OpenEMR's tabbed UI.** OpenEMR has no URL that opens a given patient's edit screen inside its tab frame. The pencil links (when `VITE_OPENEMR_WEB_URL` is set) therefore open the PHP dashboard with `set_pid` in a new tab, and editing continues there. Linking straight to `stats_full.php` could open whichever patient the OpenEMR session last had.
7. **Scope.** The photo, the encounter selector and the other PHP cards (vitals, notes, appointments, the Co-Pilot panel, and so on) are not ported. The challenge asks for the header, the five cards and one more section.

## 5. Security findings in OpenEMR

These are behaviours of this OpenEMR build (8.2.0-dev), measured on the dev stack during Phase B. The same code is deployed on Railway. We changed none of them. The port either avoids depending on each one or guards against it.

| # | Finding | Evidence | How the port handles it |
|---|---|---|---|
| 1 | **A confidential client's token exchange succeeds without its secret.** Omitting `client_secret` at `/token` issued `user/` scopes; a wrong secret gives 401. The secret is checked only when sent | `ClientRepository.php:197-216` (`if (!empty($clientSecret) && …) {…} return true;`); Phase B T6 | The BFF always sends the secret. Worth an upstream fix: it removes most of the difference between confidential and public clients |
| 2 | **Every CORS preflight to `/apis/*` returns 404**, so no cross-origin browser app can call the API with a bearer token | `RoutesExtensionListener.php:28` (priority 40) runs before `CORSListener` (25); `HttpRestRouteHandler.php:98` throws "Route not found" | This is why mode A has a BFF and modes B/C are same-origin |
| 3 | **Reading another patient returns 500, not 403/404.** `Patient/<other>` under a patient token: `500 "patient id invalid"`. Under a user token for a patient the user cannot open, also a 500 | Phase B T4 | The BFF maps that 500 to `403 not_accessible`; the app shows "You don't have permission" |
| 4 | **The lab search ignores `patient` under a patient-context token** and returns the context patient's results | Phase B T4 | The subject guard (§3.3) drops them |
| 5 | **The consent page lists scopes that `/token` then silently drops** (a public client asking for `user/` scopes) | Phase B T3a | The port does not request scopes its client type cannot get |
| 6 | `/api/patient/:puuid` returns the whole `patient_data` row, SSN included, to any token allowed to read the patient | Phase B T7 | The BFF answers only `{pid, uuid}` |
| 7 | `Patient.active` is hard-coded `true` in FHIR | `FhirPatientService.php:212` (`setActive(true)`) | Not displayed |

## 6. How to verify

- **Tests**: in `dashboard/`, `npm ci && npm run lint && npm run typecheck && npm test && npm run build && npm run build:smart`. This is the same as the CI job.
- **Live, mode A**:
  1. Register the confidential client and put its values in `dashboard/.env`.
  2. `npm run dev`, open http://localhost:5173 and sign in as a clinician. The landing page lists patients by last name; search "Demo" to narrow it; open two patients and they appear under *Recent patients*.
  3. Compare with the PHP dashboard for the same patient using `docs/dashboard-parity/PARITY.md`.
- **Live, modes B/C**: `npm run build:smart`, register the SMART client, then *Patient → Patient Dashboard (React)*.
- **Images**:
  - `docker build -t dashboard-bff dashboard` runs; the BFF refuses to start without its variables and reports healthy with them.
  - The root image was built from the committed tree and started **without** `DASHBOARD_SMART_CLIENT_ID`. OpenEMR installed, the Co-Pilot migration runner ran, `public/dashboard/` was served with its CSP, and both the launch page and the app answered "not configured" instead of failing.

## 7. What we would do next

1. **Start the card requests alongside the Patient read.** The Patient read already reports a 403 or 404, so starting the other requests in parallel would cut one API round trip (about 1 s on the dev stack) from time to cards.
2. **Store BFF sessions in Redis or signed, encrypted cookies**, so mode A can run more than one replica and survive a redeploy without signing everyone out.
3. **Report findings 1–4 upstream.** Each has a small fix:
   - require the secret for confidential clients
   - let `CORSListener` answer `OPTIONS` before routing
   - return 403/404 instead of 500 for an inaccessible patient
   - apply `patient` in the lab search under patient context
4. **Close parity gaps where FHIR can carry the data.** Candidates: allergy severity (the reaction's `severity` element), refills (`numberOfRepeatsAllowed`) and the lab procedure name (`DiagnosticReport` readable for clinicians). Each needs an OpenEMR change, which this challenge ruled out.
5. **Add a Playwright suite to CI.** It would replace the manual Selenium runs used for the parity screenshots, the real-browser axe run and the timings, so they run on every change.
