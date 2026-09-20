# oe-module-copilot

The OpenEMR side of the Clinical Co-Pilot (ARCHITECTURE.md §4–§6, §10). It is
the single trust boundary: it authorizes the physician, binds the patient,
reads clinical data, normalizes it into a `ContextBundle`, hands the bundle
to the agent over a signed server-to-server call, mints the short-lived
patient-bound ticket the panel uses, and renders the deterministic sections.
It never calls the LLM and never writes to clinical tables.

## Enable

Administration → Modules → Manage Modules → install/enable `oe-module-copilot`
(or `dev/seed_evelyn_demo.php --confirm-local --enable-module` on the dev
stack). Configuration is environment-only, never the globals table:

- `COPILOT_AGENT_URL` — base URL of the agent as reachable from the OpenEMR
  container (dev stack: `http://host.docker.internal:8765`)
- `COPILOT_TICKET_SECRET` — ≥ 32 characters, identical to the agent's

With either missing the panel still renders the deterministic sections and
reports the plan check unavailable.

Optional, same names as the agent's tracing settings: `LANGFUSE_BASE_URL`,
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` (and `COPILOT_ENVIRONMENT`, default
`development`). With all three set the module reports every ticket outcome to
Langfuse as one `ticket.<outcome>` span (issued, agent_unavailable, no_prior_note,
refused, source_unavailable, internal_error, ticket_refresh) whose trace id is the
correlation id, so it joins the agent's trace for that briefing — and a request
the agent never saw is a trace with only a ticket span. The span carries the cid
and fixed codes only; it is sent after the response with a 1 s timeout and can
never fail or delay the request (`src/Observability/`). This is the north-star
denominator in KEY_METRICS.md §3. The report goes straight to Langfuse, not via
the agent, so an agent outage is counted; a Langfuse outage is not (the audit
log is the durable record) — an external heartbeat covers that case.

One module global exists (Administration → Globals → Clinical Co-Pilot):
`copilot_admin_relationship_override`, off by default, lets admin/super
users without a care relationship read a context; every such read is audited
with basis `admin_override`.

## Demo data

`dev/seed_evelyn_demo.php --confirm-local` seeds the tracer-bullet patient;
`dev/seed_demo_patients.php --confirm-local` seeds seven synthetic patients, one per
evidence state (each mirrors a fixture case in `copilot-agent/fixtures/cases/`),
all with an appointment today so the schedule reads like a clinic morning. Both
refuse to run outside a local/compose database and are idempotent by name.
Verified 2026-09-20 through the real module and model: every patient lands on
the intended state.

## Route

`POST /api/copilot/briefing-ticket` (local API bridge; `APICSRFTOKEN` header).
Body `{pid}` — the pid is a staleness check only; the binding is always the
session's selected patient. Steps: ACL (`patients/demo`, `encounters/auth_a`,
`encounters/notes`, `patients/med`, `patients/lab`, `patients/appt`) → care
relationship (encounter provider/supervisor, appointment provider ±7 days,
primary provider) → audit row (`log`, event `copilot-briefing`, comment with
`cid`) → reads → bundle → agent → ticket → response `{correlation_id,
patient_uuid, agent_url, bundle_id, ticket, ticket_expires_at, sections,
degraded, warnings}`. Body `{pid, refresh_bundle_id, refresh_correlation_id}`
re-authorizes and mints a fresh ticket for an existing bundle (follow-up turns
after expiry) without re-reading the chart.

Sections (panel-only, never the plan text): identity (never sent to the
agent), scheduled and baseline-encounter reasons verbatim with provenance,
baseline note and window, interval results/orders, medication changes,
allergies as recorded (`no allergy entries on file (not confirmed NKA)` when
empty), footer (sources that could not be read, duplicates collapsed).

## Data access

`src/Data/SqlClinicalReader.php` reads through `QueryUtils` with bound
parameters: `patient_data`, `users.uuid`, `form_encounter`, `forms` +
`form_soap`, `procedure_order` / `procedure_order_code` / `procedure_report`
/ `procedure_result`, `openemr_postcalendar_events` (`pc_pid` compared as a
string), `prescriptions` ∪ `lists(type='medication')`, `lists(type='allergy')`.
`src/ContextBundleBuilder.php` applies the data-quality rules (empty → null,
UTC timestamps, vocabulary mapping, DATA-003 medication status, DATA-004
duplicate collapse) and declares any source that failed in
`data_quality.sources_unavailable` — never an empty list.

## Panel

`src/Panel/PanelRenderer.php` emits the mount point on the Patient Summary
(`RenderEvent::EVENT_SECTION_LIST_RENDER_TOP`); `public/copilot-panel.js`
renders everything with `textContent`, opens the agent stream with a fetch
and an `AbortController`, drops any event whose ids differ from its own,
shows verified commitments, interval annotations, withheld counts and a
question box (with the scope statement and example questions from USER.md UC-04),
and deletes the bundle on unload.

## Tests and checks

```
# inside the openemr container (openemr-cmd shell, or docker compose exec)
php vendor/bin/phpunit --bootstrap interface/modules/custom_modules/oe-module-copilot/tests/bootstrap.php interface/modules/custom_modules/oe-module-copilot/tests
php vendor/bin/phpstan analyse interface/modules/custom_modules/oe-module-copilot
php vendor/bin/phpcs --standard=phpcs.xml.dist interface/modules/custom_modules/oe-module-copilot
npx eslint interface/modules/custom_modules/oe-module-copilot/public/copilot-panel.js
```

The tests use fakes for ACL, relationships, reader, agent, audit and clocks;
`tests/AgentHandoffTest.php` pins the HMAC and ticket bytes to vectors produced
by the agent's `app/security.py`. `dev/seed_evelyn_demo.php` seeds the
synthetic patient the end-to-end checks use.

## Open items (from ARCHITECTURE.md §10, §18)

OpenEMR's `api_log` full-body logging still captures the ticket response
(deterministic sections, no plan text); ACL results are not memoized across
the six checks; the `left_nav.setPatient` abort hook is not wired (the page
reload on patient switch already tears the panel down).
