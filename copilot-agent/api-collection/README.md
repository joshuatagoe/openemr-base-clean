# Clinical Co-Pilot agent — runnable API collection (Bruno)

Every agent endpoint, runnable without reading source. Open this folder in
[Bruno](https://www.usebruno.com/) or run it headless:

```sh
# local: start the agent first (stub provider = no model calls, no key needed)
COPILOT_TICKET_SECRET=collection-check-secret-0123456789abcdef MODEL_PROVIDER=stub \
  uv run uvicorn app.main:app --port 8765
npx @usebruno/cli run api-collection --env local --env-var ticket_secret=collection-check-secret-0123456789abcdef

# deployed: the value of COPILOT_TICKET_SECRET on the Railway agent (never committed)
npx @usebruno/cli run api-collection --env deployed --env-var ticket_secret=...
```

Requests run in `seq` order: liveness (01–03) → synchronous briefing (10) →
the ticket-gated flow the OpenEMR module drives (20 store bundle with HMAC →
21 mint ticket + SSE briefing → 22 follow-up turn) → negative cases (30 bad
signature, 31 ticket for another patient, 32 expired ticket) → 40 delete.
The HMAC body signature and the HS256 ticket are computed in pre-request
scripts from the same definitions as `app/security.py`, so a grader can run
the module's whole handshake with only the shared secret.

Synthetic data only (the fixture from `fixtures/lab_followup.json`); fresh
`correlation_id`/`patient_uuid` per run. Each request's `docs` tab explains
what it exercises and what a correct answer looks like.

`tests/test_api_collection.py` runs the collection against an in-process
agent when Bruno's CLI is available, so the collection cannot drift from the
API.
