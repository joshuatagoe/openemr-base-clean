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
| Langfuse (self-hosted; traces, scores, cost — PHI masked at the agent) | https://langfuse-web-production-818f.up.railway.app/ (login required) |

**Documents**

| Document | Purpose |
|---|---|
| [AUDIT.md](AUDIT.md) | Audit of OpenEMR as found, before any Co-Pilot changes |
| [USERS.md](USERS.md) | Target user, workflow and use cases |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the Co-Pilot is built: summary, glossary, end-to-end flow, verification, tradeoffs, status |
| [KEY_METRICS.md](KEY_METRICS.md) | What success means and how each metric is measured |

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
5. Seed a demo patient with a prior plan and a later result (dev database only): run `interface/modules/custom_modules/oe-module-copilot/dev/seed_evelyn_demo.php --confirm-local` inside the OpenEMR container, then open that patient's summary.

Tests: `uv run pytest` in `copilot-agent/`; module PHPUnit inside the container per the module README.

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
