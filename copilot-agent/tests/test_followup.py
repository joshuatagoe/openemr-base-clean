"""UC-04 follow-up turns: tools, verifier, bounded loop, and the ticket-gated route.

Every case names the failure mode it guards (ARCHITECTURE.md sections 8, 9 and 15).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.contracts import (
    AllergyRecord,
    CommitmentKind,
    ContextBundle,
    EvidenceSource,
    ExtractedCommitment,
    LabOrder,
    LabOrderStatus,
    LabResult,
    LabResultStatus,
    MedicationAction,
    MedicationRecord,
    MedicationSource,
    StatementKind,
)
from app.followup import MAX_TOOL_ITERATIONS, ConversationTurn, run_turn
from app.main import app, get_provider_factory, get_settings
from app.matcher import match_evidence
from app.providers.base import ProviderUnavailableError
from app.tools import ToolOutput, run_tool, tool_definitions
from app.verifier import TurnEvidence, verify_statement
from tests.fakes import FakeProvider, answer, calls, hba1c, metformin, model_output, statement
from tests.test_handoff import bearer, post_bundle, read_events, ticket_for
from tests.test_matcher import extraction, load_context

NOTE = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)
AFTER = NOTE + timedelta(days=90)
BEFORE = NOTE - timedelta(days=150)


def rich_bundle() -> ContextBundle:
    base = load_context()
    return base.model_copy(
        update={
            "lab_results": [
                LabResult(result_id="procedure_result:9001", test_name="Hemoglobin A1c", code="4548-4", value=Decimal("8.9"), units="%", range="4.0-5.6", abnormal_flag="high", status=LabResultStatus.FINAL, observed_at=AFTER),
                LabResult(result_id="procedure_result:9002", test_name="Potassium", value=Decimal("4.1"), units="mmol/L", status=LabResultStatus.FINAL, observed_at=AFTER - timedelta(days=10)),
            ],
            "lab_orders": [LabOrder(order_id="procedure_order:5", sequence=1, test_name="Lipid Panel", status=LabOrderStatus.PENDING, ordered_at=AFTER)],
            "medications": [
                MedicationRecord(record_id="prescriptions:31", source_table=MedicationSource.PRESCRIPTIONS, drug_name="Metformin HCl 500 mg", dosage_text="1 tab BID", active=True, status_field="active,end_date", status_value="active=1,end_date=null", started_at=BEFORE, timestamp=BEFORE, timestamp_field="date_added"),
            ],
            "allergies": [AllergyRecord(record_id="lists:9", title="Penicillin", coded=False, reaction="rash", active=True)],
        },
        deep=True,
    )


def matches_for(bundle: ContextBundle):
    ext = extraction(
        ExtractedCommitment(commitment_id="c-001", kind=CommitmentKind.MEDICATION, source_span="Continue metformin.", drug_name="metformin", action=MedicationAction.CONTINUE),
        ExtractedCommitment(commitment_id="c-002", kind=CommitmentKind.LAB_TEST, source_span="Repeat HbA1c in three months.", test_name="HbA1c"),
    )
    return match_evidence(bundle, ext)


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


def test_tool_definitions_are_strict_and_closed() -> None:
    defs = tool_definitions()
    assert [d["name"] for d in defs] == ["list_commitments", "find_results", "find_orders", "find_medications", "get_baseline_note", "list_allergies"]
    assert all(d["input_schema"]["additionalProperties"] is False for d in defs)


def test_find_results_resolves_aliases_and_orders_newest_first() -> None:
    out = run_tool(rich_bundle(), [], "find_results", {"test_query": "a1c"})
    assert out.error is None and [r["record_id"] for r in out.records] == ["procedure_result:9001"]
    assert out.records[0]["value"] == "8.9" and out.records[0]["abnormal_flag"] == "high"
    panel = run_tool(rich_bundle(), [], "find_results", {"test_query": "bmp"})
    assert [r["record_id"] for r in panel.records] == ["procedure_result:9002"]


def test_find_results_since_filter_and_limit() -> None:
    out = run_tool(rich_bundle(), [], "find_results", {"test_query": "potassium", "since": (AFTER + timedelta(days=1)).date().isoformat()})
    assert out.records == [] and out.error is None
    with_limit = run_tool(rich_bundle(), [], "find_results", {"test_query": "hba1c", "limit": 1})
    assert len(with_limit.records) == 1


def test_tools_report_unavailable_sources_and_bad_arguments_as_errors_not_empty_lists() -> None:
    b = rich_bundle().model_copy(update={"data_quality": rich_bundle().data_quality.model_copy(update={"sources_unavailable": [EvidenceSource.LAB_RESULTS]})})
    assert run_tool(b, [], "find_results", {"test_query": "hba1c"}).error == "source_unavailable"
    assert run_tool(rich_bundle(), [], "find_results", {"test_query": "hba1c", "patient": "other"}).error == "invalid_arguments"
    assert run_tool(rich_bundle(), [], "find_results", {"test_query": "   "}).error in {"invalid_arguments", "unresolvable_query"}
    assert run_tool(rich_bundle(), [], "delete_everything", {}).error == "unknown_tool"


def test_find_orders_medications_note_allergies_and_commitments() -> None:
    b = rich_bundle()
    assert [r["record_id"] for r in run_tool(b, [], "find_orders", {"test_query": "lipids"}).records] == ["procedure_order:5:1"]
    meds = run_tool(b, [], "find_medications", {"drug_query": "Metformin 1000 mg"}).records
    assert [m["record_id"] for m in meds] == ["prescriptions:31"] and meds[0]["dosage_text"] == "1 tab BID"
    assert run_tool(b, [], "find_medications", {"drug_query": "metformin", "include_inactive": False}).records[0]["active"] is True
    note = run_tool(b, [], "get_baseline_note", {}).records[0]
    assert note["record_id"] == "form_soap:1001" and "HbA1c" in note["plan_text"]
    allergies = run_tool(b, [], "list_allergies", {}).records
    assert allergies[0]["record_id"] == "lists:9" and allergies[0]["coded"] is False
    empty = run_tool(load_context(), [], "list_allergies", {}).records
    assert empty == [{"record_id": "allergies:none", "statement": "no allergy entries on file (not confirmed NKA)"}]
    commitments = run_tool(b, matches_for(b), "list_commitments", {}).records
    assert [c["record_id"] for c in commitments] == ["c-001", "c-002"] and commitments[1]["state"] == "matching_result_found"


# --------------------------------------------------------------------------- #
# Verifier
# --------------------------------------------------------------------------- #


def evidence() -> TurnEvidence:
    b = rich_bundle()
    return TurnEvidence([run_tool(b, [], "find_results", {"test_query": "hba1c"}), run_tool(b, [], "find_medications", {"drug_query": "metformin"})])


def test_fact_with_supported_numbers_and_known_citation_passes() -> None:
    ok, code = verify_statement(statement("HbA1c was 8.9 % on 2026-09-08, flagged high as recorded.", "fact", "procedure_result:9001"), evidence())
    assert code is None and ok is not None
    assert ok.citations[0].record_id == "procedure_result:9001" and ok.kind is StatementKind.FACT


@pytest.mark.parametrize(
    ("text", "kind", "cited", "expected"),
    [
        ("HbA1c was 8.9 %.", "fact", [], "citation_missing"),
        ("HbA1c was 8.9 %.", "fact", ["procedure_result:999"], "citation_unknown"),
        ("HbA1c was 9.1 %.", "fact", ["procedure_result:9001"], "numeric_unsupported"),
        ("The dose should be increased.", "fact", ["prescriptions:31"], "recommendation_language"),
        ("Consider a statin.", "clarification", [], "recommendation_language"),
        ("The patient has no known allergies.", "fact", ["prescriptions:31"], "absence_as_negation"),
        ("The patient never took metformin.", "fact", ["prescriptions:31"], "absence_as_negation"),
        ("Potassium is low.", "fact", ["procedure_result:9001"], "interpretation_without_flag"),
        ("No lipid results were found.", "no_record_found", [], "absence_without_empty_search"),
        ("No lipid results were found.", "no_record_found", ["procedure_result:9001"], "absence_without_empty_search"),
    ],
)
def test_unsupported_statements_are_rejected_with_a_code(text: str, kind: str, cited: list[str], expected: str) -> None:
    ok, code = verify_statement(statement(text, kind, *cited), evidence())
    assert ok is None and code == expected


def test_no_record_found_requires_an_empty_search_and_no_citations() -> None:
    b = rich_bundle()
    ev = TurnEvidence([run_tool(b, [], "find_results", {"test_query": "tsh"})])
    ok, code = verify_statement(statement("No TSH result was found in this system.", "no_record_found"), ev)
    assert code is None and ok is not None and ok.citations == []
    ok, code = verify_statement(statement("No TSH result was found.", "no_record_found", "procedure_result:9001"), ev)
    assert code == "absence_without_empty_search" or code == "absence_with_citations"


def test_interpretation_allowed_only_when_the_flag_says_so() -> None:
    ok, code = verify_statement(statement("The HbA1c of 8.9 % is flagged high as recorded.", "fact", "procedure_result:9001"), evidence())
    assert code is None
    ok, code = verify_statement(statement("The HbA1c of 8.9 % is normal.", "fact", "procedure_result:9001"), evidence())
    assert code == "interpretation_without_flag"


def test_refusal_and_clarification_need_no_citation_but_no_advice() -> None:
    ok, code = verify_statement(statement("I can only report what is in this patient's record; I cannot give dosing advice.", "refusal"), evidence())
    assert code is None and ok is not None and ok.kind is StatementKind.REFUSAL
    ok, code = verify_statement(statement("Which test do you mean: HbA1c or potassium?", "clarification"), evidence())
    assert code is None


# --------------------------------------------------------------------------- #
# Loop
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_loop_runs_tools_then_verifies_the_answer() -> None:
    b = rich_bundle()
    provider = FakeProvider(turn_script=[
        calls(("find_results", {"test_query": "HbA1c"}), ("find_medications", {"drug_query": "metformin"})),
        answer(
            statement("The latest HbA1c was 8.9 % (flagged high as recorded).", "fact", "procedure_result:9001"),
            statement("Metformin HCl 500 mg is recorded as active, 1 tab BID.", "fact", "prescriptions:31"),
            statement("You should raise the dose.", "fact", "prescriptions:31"),
        ),
    ])
    outcome = await run_turn(provider, b, matches_for(b), [], "What was the last A1c and is she on metformin?")
    assert [s.text for s in outcome.statements] == ["The latest HbA1c was 8.9 % (flagged high as recorded).", "Metformin HCl 500 mg is recorded as active, 1 tab BID."]
    assert outcome.rejected_count == 1 and outcome.rejection_codes == ["recommendation_language"]
    assert [t.tool for t in outcome.tool_calls] == ["find_results", "find_medications"] and outcome.iterations == 1
    # The question reached the model as delimited data; tool results were fed back.
    first = provider.turn_transcripts[0]
    assert first[-1]["role"] == "user" and "<question>" in first[-1]["content"]
    second = provider.turn_transcripts[1]
    assert second[-1]["content"][0]["type"] == "tool_result" and "procedure_result:9001" in second[-1]["content"][0]["content"]


@pytest.mark.anyio
async def test_loop_is_capped_and_forces_an_answer() -> None:
    b = rich_bundle()
    provider = FakeProvider(turn_script=[calls(("find_results", {"test_query": "HbA1c"}))] * 10)
    outcome = await run_turn(provider, b, [], [], "Anything?")
    assert outcome.iterations == MAX_TOOL_ITERATIONS
    assert provider.forced == [False, False, False, True]
    assert outcome.statements == []


@pytest.mark.anyio
async def test_history_is_this_bundle_only_and_prior_answers_are_verified_text() -> None:
    b = rich_bundle()
    prior = ConversationTurn(question="Last A1c?", statements=[])
    provider = FakeProvider(turn_script=[answer(statement("Which value do you mean?", "clarification"))])
    outcome = await run_turn(provider, b, [], [prior], "And the one before?")
    transcript = provider.turn_transcripts[0]
    assert [m["role"] for m in transcript] == ["user", "assistant", "user"]
    assert "No verified statements" in transcript[1]["content"][0]["text"]
    assert outcome.statements[0].kind is StatementKind.CLARIFICATION


@pytest.mark.anyio
async def test_tool_failure_is_reported_not_fabricated() -> None:
    b = rich_bundle()
    provider = FakeProvider(turn_script=[calls(("find_results", {"test_query": "hba1c", "limit": 500})), answer(statement("No HbA1c was found.", "no_record_found"))])
    outcome = await run_turn(provider, b, [], [], "A1c?")
    assert outcome.tool_calls[0].error == "invalid_arguments"
    # An errored search is not an empty search: absence cannot be claimed from it.
    assert outcome.statements == [] and outcome.rejection_codes == ["absence_without_empty_search"]


# --------------------------------------------------------------------------- #
# Route: POST /v1/conversations/{bundle_id}/turns
# --------------------------------------------------------------------------- #


def turn(client: TestClient, bundle_id: str, token: str, question: str):
    return client.post(f"/v1/conversations/{bundle_id}/turns", json={"question": question}, headers=bearer(token))


@pytest.fixture
def scripted_provider() -> FakeProvider:
    return FakeProvider(
        model_output(metformin(), hba1c()),
        turn_script=[
            calls(("find_results", {"test_query": "HbA1c"})),
            answer(statement("The latest HbA1c result was 8.9 % on 2026-09-12.", "fact", "procedure_result:9001")),
        ],
    )


@pytest.fixture
def fake_provider(scripted_provider: FakeProvider) -> FakeProvider:  # override conftest's default for this module
    return scripted_provider


def test_turn_answers_over_the_stored_bundle_with_verified_statements(client: TestClient, fixture_payload: dict) -> None:
    accepted = post_bundle(client, fixture_payload)
    assert read_events(client, accepted["bundle_id"], ticket_for(accepted))[0] == 200  # briefing first
    token = ticket_for(accepted)
    resp = turn(client, accepted["bundle_id"], token, "What was the last A1c?")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["correlation_id"] == accepted["correlation_id"] and body["patient_uuid"] == accepted["patient_uuid"]
    assert body["turn_index"] == 1 and body["degraded"] is None
    assert [s["text"] for s in body["statements"]] == ["The latest HbA1c result was 8.9 % on 2026-09-12."]
    assert body["statements"][0]["citations"][0]["record_id"] == "procedure_result:9001"
    assert body["tool_calls"] == [{"tool": "find_results", "args": {"test_query": "HbA1c"}, "records": 1, "truncated": False, "error": None}]
    # A second turn on the same ticket is allowed within its lifetime (turns are not single-use).
    resp = turn(client, accepted["bundle_id"], token, "Thanks.")
    assert resp.status_code == 200 and resp.json()["turn_index"] == 2


def test_turn_ticket_for_bundle_a_cannot_ask_about_bundle_b(client: TestClient, fixture_payload: dict) -> None:
    a = post_bundle(client, fixture_payload)
    b = post_bundle(client, fixture_payload, correlation_id=str(uuid4()), patient_uuid=str(uuid4()))
    resp = turn(client, b["bundle_id"], ticket_for(a), "What was the last A1c?")
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "ticket_bundle_mismatch"
    resp = turn(client, a["bundle_id"], ticket_for(a, puuid=str(uuid4())), "What was the last A1c?")
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "ticket_patient_mismatch"


def test_turn_after_delete_or_with_expired_ticket_fails_closed(client: TestClient, fixture_payload: dict) -> None:
    accepted = post_bundle(client, fixture_payload)
    import time

    expired = ticket_for(accepted, iat=int(time.time()) - 300, exp=int(time.time()) - 60)
    assert turn(client, accepted["bundle_id"], expired, "A1c?").status_code == 401
    client.delete(f"/v1/bundles/{accepted['bundle_id']}", headers=bearer(ticket_for(accepted)))
    assert turn(client, accepted["bundle_id"], ticket_for(accepted), "A1c?").status_code == 404


def test_turn_rejects_overlong_or_empty_questions(client: TestClient, fixture_payload: dict) -> None:
    accepted = post_bundle(client, fixture_payload)
    assert turn(client, accepted["bundle_id"], ticket_for(accepted), "").status_code == 422
    assert turn(client, accepted["bundle_id"], ticket_for(accepted), "x" * 1001).status_code == 422


@pytest.mark.parametrize("scripted_provider", [FakeProvider(model_output(), turn_script=[ProviderUnavailableError("down")])])
def test_provider_failure_is_a_degraded_turn_not_an_answer(client: TestClient, fixture_payload: dict, scripted_provider: FakeProvider) -> None:
    accepted = post_bundle(client, fixture_payload)
    body = turn(client, accepted["bundle_id"], ticket_for(accepted), "A1c?").json()
    assert body["statements"] == [] and body["degraded"]["stage"] == "turn" and body["degraded"]["reason_code"] == "provider_unavailable"


def test_conversations_do_not_leak_between_bundles(client: TestClient, fixture_payload: dict, scripted_provider: FakeProvider) -> None:
    a = post_bundle(client, fixture_payload)
    b = post_bundle(client, fixture_payload, correlation_id=str(uuid4()), patient_uuid=str(uuid4()))
    turn(client, a["bundle_id"], ticket_for(a), "Question for A")
    scripted_provider.turn_transcripts.clear()
    turn(client, b["bundle_id"], ticket_for(b), "Question for B")
    first_b_transcript = scripted_provider.turn_transcripts[0]
    assert "Question for A" not in json.dumps(first_b_transcript)


# --------------------------------------------------------------------------- #
# Scope: out-of-scope questions are refused at once, without tool searching
# --------------------------------------------------------------------------- #

OUT_OF_SCOPE_REFUSAL = "This question is outside what the Co-Pilot can check. It answers only from this patient's results, orders, medications, allergies and the last plan."


def test_prompt_names_the_sources_and_the_no_tool_refusal() -> None:
    """Guards the contract the panel states (USERS.md UC-04): the six sources, plan-progress questions via list_commitments,
    and an immediate refusal for anything else. Failure mode: the model searches every tool for an unanswerable question
    and the turn times out instead of telling the physician what the tool can do."""
    from app.providers.prompt import FOLLOWUP_SYSTEM_PROMPT as p

    for tool in ("find_results", "find_orders", "find_medications", "list_allergies", "get_baseline_note", "list_commitments"):
        assert tool in p
    assert "Do not call any tool" in p and "kind refusal" in p
    assert OUT_OF_SCOPE_REFUSAL in p
    assert "list_commitments" in p.split("what changed")[1].split("\n")[0]


def test_out_of_scope_refusal_is_rendered_uncited_with_no_tool_calls(client: TestClient, fixture_payload: dict, scripted_provider: FakeProvider) -> None:
    """A refusal is the one statement kind that needs no citation; it must come back with tool_calls == [] and rejected_count == 0."""
    scripted_provider._turn_script = [answer(statement(OUT_OF_SCOPE_REFUSAL, "refusal"))]  # noqa: SLF001
    accepted = post_bundle(client, fixture_payload)
    resp = turn(client, accepted["bundle_id"], ticket_for(accepted), "Is her blood pressure well controlled?")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["degraded"] is None and body["tool_calls"] == [] and body["rejected_count"] == 0
    assert [(s["kind"], s["citations"]) for s in body["statements"]] == [("refusal", [])]
    assert body["statements"][0]["text"] == OUT_OF_SCOPE_REFUSAL


def test_turn_outcome_scores_refused_for_a_refusal_only_answer(client: TestClient, fixture_payload: dict, scripted_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, object]] = []
    monkeypatch.setattr("app.main.score", lambda name, value, data_type="NUMERIC": seen.append((name, value)))
    scripted_provider._turn_script = [answer(statement(OUT_OF_SCOPE_REFUSAL, "refusal"))]  # noqa: SLF001
    accepted = post_bundle(client, fixture_payload)
    assert turn(client, accepted["bundle_id"], ticket_for(accepted), "Is her blood pressure well controlled?").status_code == 200
    assert ("turn_outcome", "refused") in seen and ("turn_success", 0) in seen


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_ANTHROPIC_INTEGRATION_TEST") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="live Anthropic call; set RUN_ANTHROPIC_INTEGRATION_TEST=1 and ANTHROPIC_API_KEY to run",
)
@pytest.mark.parametrize(
    "question",
    [
        "Have there been any changes since our last encounter?",  # plan progress -> list_commitments, cited
        "Is her blood pressure well controlled?",  # vitals: out of scope -> immediate refusal
        "What did the cardiology consult say?",  # notes other than the plan: out of scope
    ],
)
def test_live_scope_behaviour(fixture_payload: dict, question: str) -> None:
    """Real model: in-scope plan-progress questions are answered from list_commitments with citations; out-of-scope
    questions get exactly one refusal with zero tool calls and well inside the turn budget."""
    import time

    from app.providers.anthropic_provider import AnthropicProvider
    from app.settings import ModelSettings

    from tests.conftest import configured_settings

    app.dependency_overrides.pop(get_provider_factory, None)
    app.dependency_overrides[get_settings] = lambda: configured_settings(briefing_timeout_seconds=10.0)  # the production budget
    try:
        with TestClient(app) as live_client:
            accepted = post_bundle(live_client, fixture_payload)
            code, _, events = read_events(live_client, accepted["bundle_id"], ticket_for(accepted))
            assert code == 200 and events[-1][0] == "complete"
            started = time.perf_counter()
            resp = turn(live_client, accepted["bundle_id"], ticket_for(accepted), question)
            elapsed = time.perf_counter() - started
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["degraded"] is None, body
    kinds = [s["kind"] for s in body["statements"]]
    tools = [t["tool"] for t in body["tool_calls"]]
    if "changes since" in question:
        assert tools == ["list_commitments"], tools
        assert all(s["citations"] for s in body["statements"] if s["kind"] == "fact"), body["statements"]
    else:
        assert tools == [] and kinds == ["refusal"], (tools, kinds, body["statements"])
        assert elapsed < 6, elapsed
