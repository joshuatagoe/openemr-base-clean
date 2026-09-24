[![Syntax Status](https://github.com/openemr/openemr/actions/workflows/syntax.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/syntax.yml)
[![Styling Status](https://github.com/openemr/openemr/actions/workflows/styling.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/styling.yml)
[![Testing Status](https://github.com/openemr/openemr/actions/workflows/test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/test.yml)
[![JS Unit Testing Status](https://github.com/openemr/openemr/actions/workflows/js-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/js-test.yml)
[![PHPStan](https://github.com/openemr/openemr/actions/workflows/phpstan.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/phpstan.yml)
[![Rector](https://github.com/openemr/openemr/actions/workflows/rector.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/rector.yml)
[![ShellCheck](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml)
[![Docker Compose Linting](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml)
[![Dockerfile Linting](https://github.com/openemr/openemr/actions/workflows/docker-lint-hadolint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/docker-lint-hadolint.yml)
[![Isolated Tests](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml)
[![Inferno Certification Test](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml)
[![Composer Checks](https://github.com/openemr/openemr/actions/workflows/composer.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer.yml)
[![Composer Require Checker](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml)
[![API Docs Freshness Checks](https://github.com/openemr/openemr/actions/workflows/api-docs.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/api-docs.yml)
[![codecov](https://codecov.io/gh/openemr/openemr/graph/badge.svg?token=7Eu3U1Ozdq)](https://codecov.io/gh/openemr/openemr)

[![Backers on Open Collective](https://opencollective.com/openemr/backers/badge.svg)](#backers) [![Sponsors on Open Collective](https://opencollective.com/openemr/sponsors/badge.svg)](#sponsors)

# Clinical Co-Pilot (AgentForge fork)

This fork adds a Clinical Co-Pilot for a primary-care physician's 90 seconds before an established-patient visit: which commitments in the last plan have evidence in the record, which are pending, which have none, plus scoped follow-up questions. Every clinical claim cites a record; a missing record is never rendered as "not done".

**Deployed**

| Service | URL |
|---|---|
| OpenEMR with the Co-Pilot panel (Patient Summary) | https://openemr-base-clean-production.up.railway.app/ |
| Co-Pilot agent ([`/health`](https://copilot-agent-production-0395.up.railway.app/health), [`/ready`](https://copilot-agent-production-0395.up.railway.app/ready) — checks the ticket secret, model provider, bundle store, OpenEMR and Langfuse, [`/docs`](https://copilot-agent-production-0395.up.railway.app/docs)) | https://copilot-agent-production-0395.up.railway.app/ |
| Langfuse (self-hosted; traces, scores, cost — PHI masked at the agent) | https://langfuse-web-production-818f.up.railway.app/ (login required) — [Clinical Co-Pilot dashboard](https://langfuse-web-production-818f.up.railway.app/project/cmu8ny4ie0006ok02zd0nib5e/dashboards/cmu8tudy10001ql02y7yb0i7r) |

**Documents**

| Document | Purpose |
|---|---|
| [AUDIT.md](AUDIT.md) | Audit of OpenEMR as found, before any Co-Pilot changes |
| [USER.md](USER.md) | Target user, workflow and use cases |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the Co-Pilot is built: summary, glossary, end-to-end flow, verification, tradeoffs, status |
| [KEY_METRICS.md](KEY_METRICS.md) | What success means and how each metric is measured |
| [EVAL.md](EVAL.md) | The evaluation dataset: every case, its class and the failure mode it guards, with deterministic and live results |
| [COST_ANALYSIS.md](COST_ANALYSIS.md) | Measured unit costs, development spend, projections at 100 / 1K / 10K / 100K physicians and the architectural changes each level needs |

**Where the code lives**

- `interface/modules/custom_modules/oe-module-copilot/` — PHP module: authorization, clinical reads, bundle building, ticket signing, panel ([README](interface/modules/custom_modules/oe-module-copilot/README.md))
- `copilot-agent/` — Python service: commitment extraction, deterministic matching, verification, SSE streaming, follow-up turns, `/metrics` ([README](copilot-agent/README.md))

**Turn on the Co-Pilot** (any deployment: local dev stack or Railway)

The module ships in this repository but is off until it is enabled, and the plan check needs a running agent that OpenEMR can reach.

1. Run the agent (`copilot-agent/`) with `ANTHROPIC_API_KEY` and a `COPILOT_TICKET_SECRET` of at least 32 characters in its environment.
2. Give OpenEMR the agent: set `COPILOT_AGENT_URL` (the agent's base URL as reachable from the OpenEMR container) and the same `COPILOT_TICKET_SECRET` in OpenEMR's environment, then restart OpenEMR. These are environment variables, never Globals.
3. Enable the module: log in as an administrator → Administration → Modules → Manage Modules → install/enable `oe-module-copilot`. The panel then appears on the Patient Summary of every patient.

Without steps 1–2 the panel still renders the deterministic sections and reports the plan check unavailable; without step 3 there is no panel. Details and the one module global (admin relationship override) are in the [module README](interface/modules/custom_modules/oe-module-copilot/README.md).

**Run locally**

1. Start OpenEMR: `cd docker/development-easy && docker compose up --detach --wait` (app at http://localhost:8300/, login `admin` / `pass`).
2. Start the agent: `cd copilot-agent && cp .env.example .env` (set `ANTHROPIC_API_KEY`, `COPILOT_TICKET_SECRET`), then `uv sync && uv run uvicorn app.main:app --port 8765`.
3. Point OpenEMR at it: set `COPILOT_AGENT_URL=http://host.docker.internal:8765` and the same `COPILOT_TICKET_SECRET` in `docker/development-easy/.env`, then recreate the `openemr` service.
4. Enable the module as above (or `dev/seed_evelyn_demo.php --confirm-local --enable-module` in the next step does it for you).
5. Seed demo patients (dev database only), inside the OpenEMR container: `interface/modules/custom_modules/oe-module-copilot/dev/seed_evelyn_demo.php --confirm-local` (the tracer-bullet patient) and `dev/seed_demo_patients.php --confirm-local` (seven synthetic patients, one per evidence state — result found, order pending, no record, result before the note, ambiguous, corrected result, no prior note — each with an appointment today). Open any of them from the calendar or patient finder. On a hosted demo instance (Railway) run the same commands from the service's Console with `--target-demo-database` added; the seeders refuse any non-local database without it and always refuse `OPENEMR__ENVIRONMENT=prod`.

Tests: `uv run pytest` in `copilot-agent/`; module PHPUnit inside the container per the module README.

**Changes since the Week 1 submission (2026-09-21, before Week 2 work)**

Everything above is the Week 1 baseline as submitted on 2026-09-20. The items below are **not Week 1 results and not Week 2 work**: they were made on 2026-09-21, after the Week 1 submission and before any Week 2 (multimodal / multi-agent) surface was added, so graders can separate all three. Each is small and is covered by the suites named. Week 2 work begins after commit `e80e740` and is described in `W2_ARCHITECTURE.md` once it exists.

| Change | Why | Where |
|---|---|---|
| Tool-routing evaluation tier: 12 labelled follow-up questions, each asked N times against the synthetic bundle, scored with a decision-level boolean rubric (routing accuracy, out-of-scope leak rate) | Which tool the model reaches for is probabilistic, so it has to be measured statistically rather than asserted by three passing examples. Last run: 1.00 (36/36), leak 0.00 (`EVAL.md`, "Tool routing"); gates in `KEY_METRICS.md` §4 | `copilot-agent/app/routing_eval.py`, `fixtures/routing_cases.json`, `tests/test_routing_eval.py`; `python -m app.eval --routing N` |
| Refusals are rendered as one of two fixed sentences (scope / advice), never as model prose | Found by the new tier: an advice question ("Should I increase her metformin dose?") was refused without tools every time, but the verifier then dropped the refusal for `recommendation_language` because it quoted the request, leaving an empty answer. The fixed sentence also closes the converse: advice labelled `refusal` to slip past the deny-list. Why this fix, how the tier saw it and why 293 prior tests did not: `ARCHITECTURE.md` §15, "Case study: the advice refusal" | `copilot-agent/app/verifier.py` (`canonical_refusal`), `app/contracts.py`, `app/providers/prompt.py`; `tests/test_followup.py` |
| Live tests never export traces | The opt-in live tests were sending test traces to the production Langfuse, and the SDK's flush on app shutdown could block a `TestClient` exit indefinitely | `copilot-agent/tests/conftest.py` |

Baseline at that point: agent suite 302 passed / 6 skipped (the 6 are the opt-in live tiers, all passing when run), module PHPUnit 57 / 57.

API collection: [`copilot-agent/api-collection/`](copilot-agent/api-collection/README.md) (Bruno) runs every agent endpoint — including the module's signed handshake and the ticket-gated briefing/follow-up flow — against a local or the deployed agent with one command.

---

## Week 2 — multimodal evidence agent

Everything in this section is Week 2 work, added after commit `e80e740`. Nothing above it changed behaviour; the Week 1 panel and briefing still work exactly as described.

**What it adds.** A lab report filed through OpenEMR's own Documents screen can now be briefed: the agent reads the document, extracts structured results with citations back to the printed text, retrieves guideline evidence, and returns a grounded briefing — *What changed*, *Needs attention*, *What to consider* — where every claim carries its tier and its source.

**Run the Week 2 flow** (deployed or local — no separate branch, service or build)

1. The Co-Pilot must already be on — steps 1–3 of *Turn on the Co-Pilot* above.
2. Open a patient → **Documents** → **Add/Upload** → file a lab PDF. A synthetic one is committed at [`copilot-agent/fixtures/documents/lab_hba1c_clean.pdf`](copilot-agent/fixtures/documents/lab_hba1c_clean.pdf); [`lab_hba1c_degraded_scan.pdf`](copilot-agent/fixtures/documents/lab_hba1c_degraded_scan.pdf) shows an obscured value being reported as unreadable rather than guessed.
3. Open the patient's **Patient Summary** → Co-Pilot panel → **Brief from latest lab document**.

The document is stored by OpenEMR, not by the Co-Pilot: the module reads the patient's newest PDF from the core `documents` table and posts it, signed, to the agent. Extracted values are shown as **not yet in the chart** — nothing is filed without a clinician (see `W2_ARCHITECTURE.md`).

**Environment variables added in Week 2** (agent service; all optional)

| Variable | Default | Purpose |
|---|---|---|
| `COPILOT_RERANKER` | `fake` | `fake` = deterministic lexical reranker, offline. `bedrock` = Cohere Rerank 3.5 via Amazon Bedrock. The panel's footer names whichever ran |
| `COPILOT_BEDROCK_REGION` | `us-west-2` | Region where Cohere Rerank 3.5 access is enabled |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | — | Only with `COPILOT_RERANKER=bedrock`; scope the key to `bedrock:Rerank` |
| `ANTHROPIC_TIMEOUT_SECONDS` | `20` | Per model call. Set `40` in production: the briefing makes two sequential calls (~18 s total), and 2 × 40 s stays inside the module's 90 s round-trip timeout |

No new variable is needed on the OpenEMR side — the document route reuses `COPILOT_AGENT_URL` and `COPILOT_TICKET_SECRET`.

**The eval gate** — [EVAL_GATE.md](EVAL_GATE.md)

- Run it from a fresh clone, no key needed: `cd copilot-agent && uv sync && uv run python scripts/eval_gate.py`
- CI job `eval-gate` in [`.gitlab-ci.yml`](.gitlab-ci.yml) runs on every push and merge request
- Block commits locally too (hooks do not come with a clone): `git config core.hooksPath .githooks`
- The regression it blocked: [merge request !1](https://labs.gauntletai.com/calebtagoe/openemr-base-clean/-/merge_requests/1)

**Observability.** Each document briefing is one Langfuse trace: `document_briefing` (root, trace id = correlation id) → `lab_extract` (generation) → `retrieval.hybrid` → `rerank` → `answer_considerations` (generation). Both model calls carry tokens and cost; the trace also carries per-encounter scores (extraction verified fraction, retrieval candidates, evidence snippets, considerations shown, claims withheld, degraded). No document text or extracted value is exported — `tests/test_tracing.py` fails the build if one is.

**Week 2 documents**

| Document | Purpose |
|---|---|
| [W2_ARCHITECTURE.md](W2_ARCHITECTURE.md) | Ingestion flow, worker graph, RAG design, eval gate, risks and tradeoffs — each component marked Built or Planned |
| [EVAL_GATE.md](EVAL_GATE.md) | Where prompts, schemas and golden set live; how to run the gate; what makes it fail; what it does and does not test |
| [KEY_METRICS.md §12](KEY_METRICS.md) | Week 2 metrics: document-briefing correctness, measured latency and cost per step, the bottleneck |

**Tests.** Agent: `uv run pytest` in `copilot-agent/` — 497 passed, 6 skipped (the opt-in live tiers), up from 302 at the end of Week 1. The eval gate runs this suite as its first stage; `tests/test_api_collection.py` needs the Bruno CLI. Module: 67 / 67 PHPUnit, up from 57.

---

# OpenEMR

[OpenEMR](https://open-emr.org) is a Free and Open Source electronic health records and medical practice management application. It features fully integrated electronic health records, practice management, scheduling, electronic billing, internationalization, free support, a vibrant community, and a whole lot more. It runs on Windows, Linux, Mac OS X, and many other platforms.

### Contributing

OpenEMR is a leader in healthcare open source software and comprises a large and diverse community of software developers, medical providers and educators with a very healthy mix of both volunteers and professionals. [Join us and learn how to start contributing today!](https://open-emr.org/wiki/index.php/FAQ#How_do_I_begin_to_volunteer_for_the_OpenEMR_project.3F)

> Already comfortable with git? Check out [CONTRIBUTING.md](CONTRIBUTING.md) for quick setup instructions and requirements for contributing to OpenEMR by resolving a bug or adding an awesome feature 😊.

### Support

Community and Professional support can be found [here](https://open-emr.org/wiki/index.php/OpenEMR_Support_Guide).

Extensive documentation and forums can be found on the [OpenEMR website](https://open-emr.org) that can help you to become more familiar about the project 📖.

### Reporting Issues and Bugs

Report these on the [Issue Tracker](https://github.com/openemr/openemr/issues). If you are unsure if it is an issue/bug, then always feel free to use the [Forum](https://community.open-emr.org/) and [Chat](https://www.open-emr.org/chat/) to discuss about the issue 🪲.

### Reporting Security Vulnerabilities

Check out [SECURITY.md](.github/SECURITY.md)

### API

Check out [API_README.md](API_README.md)

### Docker

Check out [DOCKER_README.md](DOCKER_README.md)

### FHIR

Check out [FHIR_README.md](FHIR_README.md)

### For Developers

If using OpenEMR directly from the code repository, then the following commands will build OpenEMR (Node.js version 24.* is required) :

```shell
composer install --no-dev
npm install
npm run build
composer dump-autoload -o
```

### Contributors

This project exists thanks to all the people who have contributed. [[Contribute]](CONTRIBUTING.md).
<a href="https://github.com/openemr/openemr/graphs/contributors"><img src="https://opencollective.com/openemr/contributors.svg?width=890" /></a>


### Sponsors

Thanks to our [ONC Certification Major Sponsors](https://www.open-emr.org/wiki/index.php/OpenEMR_Certification_Stage_III_Meaningful_Use#Major_sponsors)!


### License

[GNU GPL](LICENSE)
