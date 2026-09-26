"""Contract smoke test: the Wave 1 module <-> agent payloads, end to end (contracts C4, C5).

One chart open, as the module will drive it, through the real app:

1. ``POST /v1/documents/extract`` per unprocessed document; the module stores
   ``extraction`` and turns its results into candidate rows.
2. ``POST /v1/documents/briefing`` with every stored extraction + the chart's
   lab history as ``prior_facts`` - no re-extraction.
3. ``POST /v1/bundles`` with ``pending_document_facts`` built from the
   candidates, then a follow-up turn answered from them.

Then the same flow with tracing on, asserting that no new field - printed
identity, extracted values, test names, fact ids, document bytes - reaches an
exported span. Offline: StubProvider for extraction and the answer model, a
scripted turn for the follow-up.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import observability
from app.main import app, get_provider_factory, get_settings
from app.observability import trace_id_for
from app.providers.stub_provider import StubProvider
from tests.conftest import configured_settings
from tests.fakes import FakeProvider, answer, calls, statement
from tests.test_document_extract import extract_body, signed
from tests.test_followup import turn
from tests.test_handoff import post_bundle, ticket_for

PRINTED_NAME = "Whitfield, Evelyn R."
PRINTED_DOB = date(1981, 3, 14)
LABEL = "not yet verified or filed"


class _ChartOpenProvider(StubProvider):
    """Stub extraction and answer model; a scripted follow-up turn."""

    def __init__(self) -> None:
        super().__init__()
        self._turns = FakeProvider(
            turn_script=[
                calls(("find_results", {"test_query": "glucose"}), ("find_pending_document_facts", {"test_query": "glucose"})),
                answer(
                    statement("Glucose, Fasting 164 mg/dL on 2026-09-12 from document 201 is not yet verified or filed.", "fact", "copilot_extracted_value:2"),
                    statement("No glucose result was found.", "no_record_found"),  # hides the pending value: rejected
                ),
            ]
        )

    async def turn_step(self, *args: Any, **kwargs: Any) -> Any:
        return await self._turns.turn_step(*args, **kwargs)


@pytest.fixture
def with_printed_identity() -> None:
    """The fixture PDF prints "PATIENT: Whitfield, Evelyn R.  DOB: 1981-03-14"; since Lane 1 landed,
    the offline stub parses it like the real model does, so no stand-in is needed."""


def candidates_from(document_id: int, extraction: dict[str, Any], first_id: int) -> list[dict[str, Any]]:
    """What the module's ContextBundleBuilder sends for one stored extraction (C5)."""
    rows = []
    for n, r in enumerate(extraction["results"], start=first_id):
        rows.append(
            {
                "fact_id": f"copilot_extracted_value:{n}",
                "document_id": document_id,
                "test_name": r["test_name"],
                "value_text": None if r["value"] is None else str(r["value"]),
                "unit": r["unit"],
                "reference_range": r["reference_range"],
                "abnormal_flag": r["abnormal_flag"],
                "flag_source": r["abnormal_flag_source"],
                "collection_date": r["collection_date"] or extraction["collection_date"],
                "verification_status": r["verification_status"],
                "page": r["citation"]["page"],
                "bbox": r["citation"]["bbox"],
                "status": "candidate",
            }
        )
    return rows


def chart_open(client: TestClient, fixture_payload: dict, **briefing_fields: Any) -> dict[str, Any]:
    """Drive the three Wave 1 routes the way the module does on chart open."""
    body = extract_body("lab_hba1c_clean.pdf", document_id=201)
    extracted = client.post("/v1/documents/extract", content=body, headers=signed(body))
    assert extracted.status_code == 200, extracted.text
    ex = extracted.json()
    assert ex["status"] == "ok" and ex["doc_type"] == "lab_pdf"

    context = fixture_payload["context"]
    stored = [{"document_id": 201, "doc_type": "lab_pdf", "extraction": ex["extraction"]}]
    briefing_payload = {
        "correlation_id": context["correlation_id"],
        "patient_uuid": ex["patient_uuid"],
        "documents": stored,
        "prior_facts": context["lab_results"],
        "question": None,
        **briefing_fields,
    }
    body = json.dumps(briefing_payload).encode()
    briefed = client.post("/v1/documents/briefing", content=body, headers=signed(body))
    assert briefed.status_code == 200, briefed.text

    pending = candidates_from(201, ex["extraction"], first_id=1)
    accepted = post_bundle(client, fixture_payload, pending_document_facts=pending)
    answered = turn(client, accepted["bundle_id"], ticket_for(accepted), "What did the new report say about glucose?")
    assert answered.status_code == 200, answered.text
    return {"extract": ex, "briefing": briefed.json(), "accepted": accepted, "turn": answered.json(), "pending": pending}


def _client_with(provider: Any) -> Iterator[TestClient]:
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    app.dependency_overrides[get_settings] = lambda: configured_settings()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_provider_factory, None)
        app.dependency_overrides.pop(get_settings, None)


# --------------------------------------------------------------------------- #
# The contract, end to end
# --------------------------------------------------------------------------- #


def test_chart_open_extract_brief_and_follow_up_over_the_wave_1_contracts(fixture_payload: dict, with_printed_identity: None) -> None:
    for client in _client_with(_ChartOpenProvider()):
        out = chart_open(client, fixture_payload)

    ex = out["extract"]
    assert ex["printed_identity"] == {"name": PRINTED_NAME, "dob": PRINTED_DOB.isoformat()}
    # ADR-012: identity is returned once, top-level, never inside the extraction the module stores.
    assert ex["extraction"].get("printed_identity") is None and PRINTED_NAME not in json.dumps(ex["extraction"])
    assert ex["extraction"]["document_id"] == 201 and ex["prompt_version"] and ex["extraction_model"]

    b = out["briefing"]
    assert b["status"] == "ok" and b["document_ids"] == [201]
    assert [s["target"] for s in b["routing"]] == ["evidence-retriever", "answer", "finish"]  # no re-extraction
    cited = {line["document_citation"]["source_id"] for line in b["briefing"]["what_changed"] + b["briefing"]["needs_attention"]}
    assert cited == {"201"}
    # The printed identity is for the module's comparison only; it never reaches the briefing.
    assert PRINTED_NAME not in json.dumps(b) and PRINTED_DOB.isoformat() not in json.dumps(b)

    t = out["turn"]
    assert t["degraded"] is None
    assert [s["text"] for s in t["statements"]] == ["Glucose, Fasting 164 mg/dL on 2026-09-12 from document 201 is not yet verified or filed."]
    assert t["statements"][0]["citations"][0]["record_type"] == "pending_document_fact"
    assert t["rejected_count"] == 1  # "No glucose result was found." beside a pending value
    assert [c["tool"] for c in t["tool_calls"]] == ["find_results", "find_pending_document_facts"]


def test_an_intake_form_is_extracted_without_blocking_the_chart(fixture_payload: dict) -> None:
    """Wave 2 (ADR-010): an intake form is read, and the chart-open flow is unaffected."""
    from tests.test_document_extract import intake_body

    for client in _client_with(_ChartOpenProvider()):
        body = intake_body()
        r = client.post("/v1/documents/extract", content=body, headers=signed(body)).json()
        out = chart_open(client, fixture_payload)
    assert r["status"] == "ok" and r["extraction"]["doc_type"] == "intake_form"
    assert out["briefing"]["status"] == "ok"


def test_documents_left_out_at_the_cap_arrive_as_a_count_and_are_stated(fixture_payload: dict) -> None:
    """Milestone 3: the module sends how many waiting documents the 20-slot cap left out; a count only."""
    for client in _client_with(_ChartOpenProvider()):
        out = chart_open(client, fixture_payload, documents_not_included=4)
    b = out["briefing"]
    assert b["status"] == "ok"
    want = "4 older document(s) with values not yet reviewed were not included in this briefing; review them in the document list."
    assert want in b["briefing"]["limitations"] and want in b["rendered_text"]


def test_an_old_undated_document_arrives_with_received_at_and_is_one_needs_attention_line(fixture_payload: dict) -> None:
    """Milestone 4: the module sends each document's upload date; an unreviewed document more than
    12 months old is one Needs attention line, not new facts. No collection date here, so the upload
    date decides (a date years back, so the real clock cannot flip this test)."""
    for client in _client_with(_ChartOpenProvider()):
        body = extract_body("lab_hba1c_clean.pdf", document_id=201)
        ex = client.post("/v1/documents/extract", content=body, headers=signed(body)).json()
        extraction = {**ex["extraction"], "collection_date": None,
                      "results": [{**r, "collection_date": None} for r in ex["extraction"]["results"]]}
        payload = {
            "correlation_id": fixture_payload["context"]["correlation_id"],
            "patient_uuid": ex["patient_uuid"],
            "documents": [{"document_id": 201, "doc_type": "lab_pdf", "extraction": extraction, "received_at": "2020-01-15"}],
            "prior_facts": [],
            "question": None,
            "documents_not_included": 0,
        }
        body = json.dumps(payload).encode()
        r = client.post("/v1/documents/briefing", content=body, headers=signed(body))
    assert r.status_code == 200, r.text
    b = r.json()["briefing"]
    assert b["what_changed"] == []
    n = len(extraction["results"])
    assert [(ln["line_id"], ln["text"], ln["document_citation"]["source_id"]) for ln in b["needs_attention"]] == [
        ("aged-201", f"An older document (received 2020-01-15) has {n} value(s) nobody has reviewed.", "201")
    ]


# --------------------------------------------------------------------------- #
# Tracing: no new field leaks into a span
# --------------------------------------------------------------------------- #


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exp = InMemorySpanExporter()
    yield exp
    observability.shutdown_tracing()


@pytest.fixture
def traced(exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    original = observability.configure_tracing
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    public_key = f"pk-lf-test-{uuid4().hex}"

    def configure_with_memory_exporter(**kwargs: Any) -> bool:
        kwargs.update(enabled=True, public_key=public_key, secret_key="sk-lf-test", base_url="http://127.0.0.1:9", span_exporter=exporter)
        return original(**kwargs)

    monkeypatch.setattr("app.main.configure_tracing", configure_with_memory_exporter)
    yield from _client_with(_ChartOpenProvider())


def test_no_new_contract_field_reaches_an_exported_span(
    traced: TestClient, exporter: InMemorySpanExporter, fixture_payload: dict, with_printed_identity: None
) -> None:
    out = chart_open(traced, fixture_payload)
    observability._langfuse.flush()  # type: ignore[union-attr]
    spans = list(exporter.get_finished_spans())
    names = {s.name for s in spans}
    assert {"document_extract", "document_briefing", "evidence-retriever", "answer", "turn", "tool"} <= names
    assert "intake-extractor" not in names  # stored extractions are never re-read

    # The extract route is one trace per correlation id, with codes and counts only.
    extract_span = next(s for s in spans if s.name == "document_extract")
    extract_cid = out["extract"]["correlation_id"]
    assert format(extract_span.context.trace_id, "032x") == trace_id_for(extract_cid)
    meta = {k.rsplit(".", 1)[-1]: v for k, v in extract_span.attributes.items() if "metadata" in k}
    assert meta.get("outcome") == "ok" and meta.get("stage") == "lab_pdf" and meta.get("records") == 3

    needles = {PRINTED_NAME, PRINTED_DOB.isoformat(), "Evelyn", out["extract"]["patient_uuid"]}
    # Values are covered by test_tracing's document test ("8.9"); bare short numbers here
    # would collide with timings and token counts, so the identifying strings are checked.
    for fact in out["pending"]:
        needles |= {fact["fact_id"], fact["test_name"]}
    for prior in fixture_payload["context"]["lab_results"]:
        needles |= {prior["result_id"], prior["test_name"]}
    needles |= {"Hemoglobin A1c", "Glucose", LABEL, "JVBERi0"}
    for s in spans:
        blob = json.dumps(dict(s.attributes), default=str)
        for needle in needles:
            assert needle not in blob, (s.name, needle)
