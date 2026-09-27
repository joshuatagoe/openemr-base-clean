# Patient dashboard (React port)

A React + TypeScript port of OpenEMR's patient summary dashboard, reading OpenEMR only through its OAuth2/OIDC-protected FHIR and standard REST APIs. No OpenEMR PHP or SQL is changed.

| Folder | What |
|---|---|
| `web/` | Vite + React + TypeScript SPA. Data access goes through a `DataSource` interface with two transports: `BffDataSource` (mode A, used now) and `SmartDataSource` (modes B/C, stub until C5). |
| `bff/` | Fastify + TypeScript backend-for-frontend: serves the built SPA, runs the OAuth login as a **confidential client**, keeps tokens server-side, and proxies an allow-list of read-only GET routes. |

Why a BFF: on this OpenEMR build a public (browser) client cannot be granted clinician `user/` scopes, and every CORS preflight to `/apis/*` returns 404, so a browser on another origin cannot call the API at all. Both were measured on the dev stack.

## Scripts (run in `dashboard/`)

| Command | Does |
|---|---|
| `npm ci` | Install both workspaces |
| `npm run dev` | BFF on :3000 (reads `dashboard/.env`) + Vite on :5173. Vite proxies `/auth`, `/api` and `/healthz` to the BFF, so the browser sees one origin. Open http://localhost:5173 |
| `npm run build` | `web/dist` (static app) and `bff/dist` (server) |
| `npm start` | Run the built BFF; it also serves `web/dist` |
| `npm test` | Vitest: BFF (mock OpenEMR on a loopback port) and web (React Testing Library + MSW) |
| `npm run lint` / `npm run typecheck` | ESLint / `tsc --noEmit` for both packages |

## Configuration (BFF environment variables)

Copy `.env.example` to `.env` (git-ignored). Required:

| Variable | Meaning |
|---|---|
| `OPENEMR_BASE_URL` | OpenEMR origin, e.g. `https://localhost:9300`. The FHIR base is `<base>/apis/default/fhir` |
| `OAUTH_ISSUER` | `site_addr_oath` + `/oauth2/default`, e.g. `https://localhost:9300/oauth2/default`. Must equal the id_token `iss` |
| `CLIENT_ID`, `CLIENT_SECRET` | The confidential client registered below. The secret stays server-side and is **always** sent at the token step |
| `REDIRECT_URI` | Exactly as registered, e.g. `http://localhost:5173/auth/callback`. Its path becomes the callback route |
| `SESSION_SECRET` | 32+ random characters; signs the session cookie |
| `COOKIE_SECURE` | `true` (default: `Secure`, `__Host-` cookies, HSTS). `false` only for plain-http local dev |

Optional: `PORT` (3000), `HOST` (127.0.0.1), `OPENEMR_SITE` (default), `UPSTREAM_TIMEOUT_MS` (10000), `LOG_LEVEL` (info), `WEB_DIST_DIR` (relative paths resolve against the working directory), `OAUTH_SCOPES`.

Web build variables:
- `VITE_DATE_DISPLAY_FORMAT` mirrors OpenEMR's `date_display_format` global: `0` = YYYY-MM-DD (default; OpenEMR's default and the dev stack's setting), `1` = MM/DD/YYYY, `2` = DD/MM/YYYY. Set it to the same value as the OpenEMR site when building.
- `VITE_OPENEMR_WEB_URL` (optional): OpenEMR's web origin, used for the cards' edit links (see *Clinical cards*). Only `http(s)` URLs are accepted; unset means no edit links.

The dev stack uses a self-signed certificate. Trust it rather than switching TLS checks off: export it once (`openssl s_client -connect localhost:9300 -showcerts </dev/null | openssl x509 > openemr-dev.pem`, keep it outside the repo) and start the BFF with `NODE_EXTRA_CA_CERTS=/path/to/openemr-dev.pem` set in the shell.

## Register the OAuth client (one-time admin step)

1. Globals (*Administration → Config → Connectors*): FHIR REST API and standard REST API on, and **Site Address Override** (`site_addr_oath`) set to the public https origin. The dev-easy stack already has these (`https://localhost:9300`).
2. Register a **confidential** client with `curl` (browser registration fails the CORS preflight):

   ```sh
   curl -k -H 'Content-Type: application/json' https://localhost:9300/oauth2/default/registration -d '{
     "application_type": "private",
     "token_endpoint_auth_method": "client_secret_post",
     "client_name": "Patient Dashboard BFF (dev)",
     "redirect_uris": ["http://localhost:5173/auth/callback"],
     "post_logout_redirect_uris": ["http://localhost:5173/"],
     "contacts": ["dev@example.com"],
     "scope": "openid fhirUser api:oemr api:fhir user/Patient.rs user/AllergyIntolerance.rs user/Condition.rs user/MedicationRequest.rs user/CareTeam.rs user/Observation.rs user/Practitioner.rs user/Organization.rs user/medication.rs user/patient.rs"
   }'
   ```

   Keep `client_id` and `client_secret` from the response in your `.env` or secret store, never in Git. `application_type` must be `private`: OpenEMR refuses `user/` scopes for public clients.
3. The client is created **disabled**. Enable it in *Administration → System → API Clients → Edit → Enable Client*.
4. Sign in with a clinician account (the dev stack's test physician is `drdash`, in the Physicians group). The consent page follows the OpenEMR login.

Scopes are read-only, with no `offline_access`, so no refresh token is issued. A session lasts as long as the access token (1 h); after that the API answers 401 and the app asks the user to sign in again.

## Web app

- **Data layer**: TanStack Query hooks (`web/src/data/hooks.ts`) over the `DataSource` interface, typed with `@types/fhir` R4. Components receive the state as data (`idle` / `loading` / `error` / `empty` / `ready`, `web/src/data/queryView.ts`). Only transient failures (502/5xx, 504, network) are retried, twice; 400/401/403/404 never are. A 401 ends the session ("Your session has expired"), and the query cache is cleared whenever the user is signed out.
- **Patient search** (`/dashboard`): name, date of birth and/or MRN, checked in the browser against the same patterns the BFF allow-lists. Criteria live in the URL (`/dashboard?name=Demo`), so Back returns to the results. The results table shows name, DOB, sex and MRN, and each name links to `/patient/:id`. Switching patients keeps the session. OpenEMR's `identifier` search also matches SSNs; the SSN is never displayed.
- **Patient header** (`/patient/:id`): parity with OpenEMR's persistent patient bar (`patient_data_template.php`, `demographics.php` `setMyPatient`): `First Last (pubpid)`, then `DOB: <date> Age: <age>` or `DOB: <date> Age at death: <age>`. The MRN is the identifier with type code `PT`, chosen by code, never by position (OpenEMR emits the SSN identifier first). Age follows `PatientService::getPatientAge` (whole years above 24 full months, else `n month`) and age at death follows `oeFormatAge`. The bar is sticky while scrolling. Focus moves to the page heading after each navigation.
- **Deliberate differences from the PHP bar**: sex and active status are not shown (the PHP bar shows neither; OpenEMR's FHIR `active` is hard-coded `true`). The photo, encounter controls and the close icon are not ported ("Find another patient" replaces the close icon). `age_display_format = 1` (`#y #m #d`) is not supported. For an age at death under 24 months the PHP prints `11months` (an operator-precedence slip); the port prints `11 months`.

## Clinical cards (C3)

Five cards under the patient bar, in the PHP page order (`demographics.php`): **Allergies**, **Medical Problems** and **Medications** side by side (three equal columns from 768 px, like `col-md-4`), then **Prescriptions** and **Care Team** full width. Below 768 px everything stacks. Titles, empty texts, columns and ordering follow `DASHBOARD_ANALYSIS_A1_PARITY.md` §2–§6 and the live PHP page.

| Card | Data | Shows |
|---|---|---|
| Allergies | `AllergyIntolerance?patient=` | `Title (criticality)`; high criticality highlighted (PHP: `bg-warning`, bold); tooltip `<title> Reaction: <reaction> - <severity>`; server order (PHP has no sort); active only |
| Medical Problems | `Condition?patient=&category=problem-list-item` **and** `…&category=encounter-diagnosis` | Title only; per-encounter copies merged (same id, or same code / free text and same onset); ended problems hidden; onset ascending, no onset first |
| Medications | `GET /api/patient/:pid/medication` rows, plus their `MedicationRequest` for the dosage | `title dosage`; PHP issue filter (outcome not Resolved, end date empty or in the future); begdate ascending, no begdate first |
| Prescriptions | the `MedicationRequest`s whose id is **not** a medication-list uuid | Drug / Details / Qty / Refills / Filled; active and completed only; newest modified first; `None` only when there are none at all, headers only when all are inactive (as PHP) |
| Care Team | `CareTeam?patient=`, then `Practitioner/:id` and `Organization/:id` per member | First active team (else the first that is not entered-in-error); team name + status badge; Type / Member / Role / Facility / Since / Status / Note |

- **States**: each card shows "Loading …", its empty text, the data, or an error. A 403 says "You don't have permission to view …"; other failures say "… could not be loaded." with **Try again** for transient ones. A 401 ends the session (C2 behaviour).
- **Medication split**: the numeric pid comes from `/api/patient/:puuid` (C2). The medication-list route answering 404 means "no list medications". If the pid lookup or the list is **forbidden** (or the transport has no standard API), the Medications and Prescriptions cards are replaced by one card titled **"Medications and prescriptions (combined)"** that says why and lists every current order once. The port never guesses a split from `intent`.
- **Subject guard**: every FHIR result must reference `Patient/<selected id>`, and every standard-API row must carry the selected pid. Anything else is dropped, and the browser console gets one line with the count and the resource type only (Phase B saw a search ignore its `patient` parameter).
- **Collapse**: the card title is a toggle button (`aria-expanded`). Defaults as in PHP for a user with no saved setting: Care Team collapsed, the others expanded. The choice is kept per browser in `localStorage` under the PHP setting names (`allergy_ps_expand`, …). PHP keeps it per user on the server, which this read-only app cannot write.
- **Edit links**: when `VITE_OPENEMR_WEB_URL` is set at build time, each card has a pencil link (new tab); otherwise there are none. Prescriptions links to `controller.php?prescription&list&id=<pid>`, the PHP pencil's own target, which names the patient. Allergies, Medical Problems, Medications and Care Team link to `interface/patient_file/summary/demographics.php?set_pid=<pid>`, the PHP dashboard for this patient, **not** straight to `stats_full.php`: that screen reads the patient from the OpenEMR session, so a direct link could open whichever patient the user last had open. OpenEMR has no deep link into its tabbed UI, so the dashboard opens on its own; its pencils call `top.restoreSession()`, which only exists inside the tab frame, and do nothing there. Editing then continues from the chart in OpenEMR. Links are shown to everyone who can see the card; OpenEMR enforces write access.

### FHIR gaps and deliberate differences (cards)

G-numbers refer to `DASHBOARD_ANALYSIS_A2_FHIR.md` §6; G21 is new in C3.

| # | PHP card | This port | Why |
|---|---|---|---|
| G4 | Allergy severity from `severity_ccda` (Mild … Fatal); highlight for severe, life threatening, fatal | FHIR `criticality`: "Low Risk" (mild, mild to moderate, moderate), "High Risk" (moderate to severe and worse, highlighted), "Unassigned" (unassigned) | OpenEMR's FHIR emits only the criticality. "Moderate to severe" is highlighted too (the safe direction) |
| — | Allergy with no severity: `Title ()` | `Title` | nothing to show |
| — | Reaction "Unassigned" in the allergy tooltip | empty | OpenEMR's FHIR omits unassigned reactions |
| — | Allergy with an end date in the **future** still listed | hidden | FHIR maps any end date without outcome Resolved to `inactive` and does not emit the date |
| — | Empty allergies: "No Known Allergies" when the list was marked reviewed (`lists_touch`), else "Nothing Recorded". Problems / medications: "None" vs "Nothing Recorded" | always "Nothing Recorded" | FHIR cannot say whether the list was reviewed, so the port never asserts "No Known Allergies" |
| G3 | Problem title = `lists.title` | a coded problem shows the code's display text | OpenEMR's FHIR drops the free-text title when a code is present |
| G3 | One row per problem | per-encounter `encounter-diagnosis` copies merged by id, else by code / text + onset | heuristic: two different problems with the same code and onset would merge |
| G21 | Problems with outcome Resolved hidden | FHIR `resolved` problems shown, marked "(Resolved)" | OpenEMR also emits `resolved` for occurrence "First" (an open first episode, which PHP lists); hiding them could hide active problems |
| G2 | Medications = `lists` rows | the same rows (standard API) with the dosage from the matching `MedicationRequest`; a list row linked to a prescription shows no dosage | OpenEMR's FHIR leaves linked list rows out; the PHP card lists them |
| G8 | Refills = `prescriptions.refills` | "—" (tooltip: not available) | OpenEMR's FHIR always sends `numberOfRepeatsAllowed: 0` |
| G7 | Details = size + unit + `dosage`, or "`<dosage> in <form> <interval>`" | dose quantity + unit, dosage text, timing text | FHIR has no drug form, and OpenEMR drops a numeric `dosage` |
| — | Drug = `prescriptions.drug` | `medicationCodeableConcept.text`, else the RxNorm display | OpenEMR sends the RxNorm display for coded prescriptions (the drug name when the code has no description) |
| — | Filled = raw `date_added` (`2026-01-15 09:00:00`) | the same text, from `authoredOn` before its offset | OpenEMR writes the server's local time plus its offset |
| — | Prescription order `active DESC, date_modified DESC, date_added DESC` | `meta.lastUpdated` (= date_modified) desc, then `authoredOn` desc | every row shown is active |
| — | "Prescription History" title with eRx; Rx card hidden by `disable_prescriptions`; cards hidden by `hide_dashboard_cards` or issue ACLs | always shown, titled "Prescriptions" | globals are not readable over the API; a 403 shows the permission message instead |
| G9 | Member name "Last, First" | the same when `Practitioner/:id` is readable. For the Physicians group (mode A) it answers 403: **"Name not available (permission)"**; facility names likewise; related persons "Name not available" | ACL `admin/users` on Practitioner / Organization. The dashboard never works around it |
| G10 | Role = the care-team role | FHIR role display, where the user's provider type wins over the care-team role | OpenEMR's mapping |
| G11 / G12 | Member Status and Note shown; inactive / entered-in-error members hidden | Status and Note "—"; **inactive members are listed** | OpenEMR's FHIR emits neither the member status nor the note |
| — | A1 expected blank Role / Facility / Status in PHP view mode (bug) | Role, Facility and Since filled from FHIR (user decision) | on the dev stack the PHP card does fill them (from its edit selects), so this is a change of data source, not a visible fix |
| — | No care team: table header, no rows | "Nothing Recorded" | explicit empty state |
| — | Each member's facility appears again as an Organization participant | skipped | not a person; the facility is on the member's row |

## BFF behaviour

- **Login**: `GET /auth/login` redirects to OpenEMR with authorization code + PKCE (S256), `state`, `nonce` and `aud`. The login is bound to the browser by a signed, 10-minute cookie. The callback checks `state` (single use), exchanges the code with the client secret and PKCE verifier, and checks the id_token `iss`, `aud`, `exp` and `nonce`.
- **Session**: an in-memory store keyed by a random id, sent as an HttpOnly, SameSite=Lax, signed cookie (`__Host-dash_sid` when `COOKIE_SECURE=true`). `GET /auth/me` returns the display name only. `POST /auth/logout` deletes the session. OpenEMR has no revocation endpoint, so the token is dropped and expires upstream. Restarting the BFF signs everyone out.
- **Display name**: OpenEMR's id_token has no `name` claim (it drops `profile`), so the BFF reads the user's own `fhirUser` Practitioner with the user's token. The Physicians group gets 403 there, so they see "OpenEMR user".
- **Proxy** (GET only; anything else is 404; unknown or repeated query parameters are 400):

  | BFF route | OpenEMR | Parameters |
  |---|---|---|
  | `/api/fhir/Patient/:id` | FHIR read | none |
  | `/api/fhir/Patient` | FHIR search | `name`, `birthdate`, `identifier` (at least one) |
  | `/api/fhir/AllergyIntolerance`, `MedicationRequest`, `CareTeam` | FHIR search | `patient` (required) |
  | `/api/fhir/Condition` | FHIR search | `patient` (required), `category` ∈ problem-list-item, encounter-diagnosis, health-concern |
  | `/api/fhir/Observation` | FHIR search | `patient` (required), `category=laboratory` (required) |
  | `/api/fhir/Practitioner/:id`, `/api/fhir/Organization/:id` | FHIR read | none |
  | `/api/patient/:pid/medication` | standard API | none (OpenEMR's 404 means "no list medications") |
  | `/api/patient/:puuid` | standard API | none; `puuid` must be a uuid. Maps the FHIR Patient id to the numeric pid the medication route needs. OpenEMR answers the whole `patient_data` row (SSN included); the BFF answers **only** `{ "pid": "7", "uuid": "..." }` and 502 when there is no numeric pid |

  Browser cookies and headers are not forwarded; the BFF adds only the bearer token. OpenEMR's 401 ends the session. Its `500` on a read of an inaccessible patient becomes `403 {"error":"not_accessible"}`. Other 5xx become 502, and a timeout becomes 504. API responses are `Cache-Control: no-store`.
- **Headers**: CSP (`default-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'none'`), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, and HSTS when cookies are Secure.
- **Logs**: one JSON line per request with the request id, method, route pattern, FHIR resource type, status and duration. URLs, query strings, headers, cookies, tokens and bodies are never logged. `GET /healthz` returns `{"status":"ok"}`.
