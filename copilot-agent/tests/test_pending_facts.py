"""Follow-ups over pending document facts (ADR-011, contract C5).

Pending facts are values read from uploaded documents that no clinician has
verified or filed yet. The follow-up path may answer from them, but:

- they arrive in their own bundle section and their own tool, so they never
  look like chart rows;
- every statement citing one carries the "not yet verified or filed" label and
  never presents it as chart data;
- "no result on file" cannot be said while a pending value for that test exists
  without saying so;
- a pending value that disagrees with a filed value is shown as a conflict,
  with both cited.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.contracts import ContextBundle, PendingDocumentFact, RecordType, StatementKind
from app.followup import run_turn
from app.providers.prompt import FOLLOWUP_SYSTEM_PROMPT
from app.tools import PENDING_LABEL, run_tool, serialize_output, tool_definitions
from app.verifier import TurnEvidence, verify_statement
from tests.fakes import FakeProvider, answer, calls, statement
from tests.test_followup import AFTER, rich_bundle

A1C_PENDING = "copilot_extracted_value:41"
LDL_PENDING = "copilot_extracted_value:42"
A1C_CHART = "procedure_result:9001"


def pending_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "fact_id": A1C_PENDING,
        "document_id": 201,
        "test_name": "Hemoglobin A1c",
        "value_text": "9.8",
        "unit": "%",
        "reference_range": "4.0-5.6",
        "abnormal_flag": None,
        "flag_source": "unavailable",
        "collection_date": AFTER.date().isoformat(),
        "verification_status": "verified_exact",
        "page": 1,
        "bbox": [0.1, 0.2, 0.3, 0.25],
        "status": "candidate",
    }
    row.update(over)
    return row


def pending_bundle(*rows: dict[str, Any]) -> ContextBundle:
    rows = rows or (
        pending_row(),
        pending_row(
            fact_id=LDL_PENDING,
            test_name="LDL Cholesterol",
            value_text="131",
            unit="mg/dL",
            reference_range="0-99",
            abnormal_flag="H",
            flag_source="derived",
            collection_date="2026-09-01",
            page=2,
            bbox=None,
        ),
    )
    data = rich_bundle().model_dump(mode="json")
    data["pending_document_facts"] = list(rows)
    return ContextBundle.model_validate(data)


def turn_evidence(bundle: ContextBundle, *tool_calls: tuple[str, dict[str, Any]]) -> TurnEvidence:
    return TurnEvidence([run_tool(bundle, [], name, args) for name, args in tool_calls])


# --------------------------------------------------------------------------- #
# Contract C5
# --------------------------------------------------------------------------- #


def test_a_bundle_carries_pending_facts_under_schema_1_0() -> None:
    bundle = pending_bundle()
    assert bundle.schema_version == "1.0"
    assert [f.fact_id for f in bundle.pending_document_facts] == [A1C_PENDING, LDL_PENDING]
    assert bundle.pending_document_facts[0].bbox == (0.1, 0.2, 0.3, 0.25)


def test_a_bundle_without_the_field_is_unchanged() -> None:
    assert rich_bundle().pending_document_facts == []


@pytest.mark.parametrize(
    "over",
    [
        {"status": "filed"},
        {"status": "rejected"},
        {"fact_id": "procedure_result:9001"},
        {"fact_id": "copilot_extracted_value:"},
        {"page": None},  # a bbox needs its page
        {"bbox": [0.5, 0.2, 0.3, 0.25]},  # x0 > x1
        {"verification_status": "guessed"},
        {"flag_source": "model"},
        {"abnormal_flag": "high"},  # the printed vocabulary is H/L/HH/LL/A/N
        {"document_id": 0},
        {"patient_name": "nobody"},
    ],
)
def test_a_malformed_pending_fact_is_refused(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PendingDocumentFact.model_validate(pending_row(**over))


def test_the_agent_accepts_a_signed_bundle_with_pending_facts(client: Any, fixture_payload: dict) -> None:
    """Agent deploys first (ADR-011 §3): the new bundle must be accepted, not 422."""
    from tests.test_handoff import post_bundle

    fixture_payload["pending_document_facts"] = [pending_row()]
    accepted = post_bundle(client, fixture_payload)  # asserts 201
    assert accepted["bundle_id"]


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #


def test_the_pending_tool_is_offered_only_when_there_are_pending_facts() -> None:
    assert "find_pending_document_facts" not in [d["name"] for d in tool_definitions()]
    defs = tool_definitions(include_pending=True)
    assert [d["name"] for d in defs][-1] == "find_pending_document_facts"
    assert all(d["input_schema"]["additionalProperties"] is False for d in defs)


def test_pending_facts_come_back_labelled_with_their_document_and_location() -> None:
    out = run_tool(pending_bundle(), [], "find_pending_document_facts", {"test_query": "a1c"})
    assert out.error is None and [r["record_id"] for r in out.records] == [A1C_PENDING]
    r = out.records[0]
    assert r["label"] == PENDING_LABEL == "not yet verified or filed"
    assert (r["value"], r["units"], r["range"], r["date"]) == ("9.8", "%", "4.0-5.6", AFTER.date().isoformat())
    assert (r["document_id"], r["page"], r["verification_status"]) == (201, 1, "verified_exact")
    assert r["source"] == "uploaded document"


def test_all_pending_facts_are_listed_when_no_test_is_named() -> None:
    out = run_tool(pending_bundle(), [], "find_pending_document_facts", {"test_query": None})
    assert {r["record_id"] for r in out.records} == {A1C_PENDING, LDL_PENDING}


def test_a_derived_flag_is_never_presented_as_a_recorded_flag() -> None:
    out = run_tool(pending_bundle(), [], "find_pending_document_facts", {"test_query": "ldl"})
    r = out.records[0]
    assert r["abnormal_flag"] is None and r["flag_source"] == "derived"


def test_a_printed_flag_is_carried_in_the_week_1_vocabulary() -> None:
    bundle = pending_bundle(pending_row(abnormal_flag="H", flag_source="extracted"))
    r = run_tool(bundle, [], "find_pending_document_facts", {"test_query": "a1c"}).records[0]
    assert r["abnormal_flag"] == "high" and r["flag_source"] == "extracted"


def test_chart_results_never_include_pending_rows_but_count_them() -> None:
    out = run_tool(pending_bundle(), [], "find_results", {"test_query": "hba1c"})
    assert [r["record_id"] for r in out.records] == [A1C_CHART]
    assert out.pending_count == 1
    assert json.loads(serialize_output(out))["pending_count"] == 1


def test_a_chart_search_with_no_pending_facts_serialises_as_before() -> None:
    out = run_tool(rich_bundle(), [], "find_results", {"test_query": "hba1c"})
    assert "pending_count" not in json.loads(serialize_output(out))


def test_a_pending_value_that_disagrees_with_a_filed_value_on_the_same_day_names_the_conflict() -> None:
    a1c = run_tool(pending_bundle(), [], "find_pending_document_facts", {"test_query": "a1c"}).records[0]
    assert a1c["conflicts_with"] == [A1C_CHART]
    ldl = run_tool(pending_bundle(), [], "find_pending_document_facts", {"test_query": "ldl"}).records[0]
    assert ldl["conflicts_with"] == []


def test_the_same_value_on_the_same_day_is_not_a_conflict() -> None:
    bundle = pending_bundle(pending_row(value_text="8.9"))
    assert run_tool(bundle, [], "find_pending_document_facts", {"test_query": "a1c"}).records[0]["conflicts_with"] == []


# --------------------------------------------------------------------------- #
# Verifier rules
# --------------------------------------------------------------------------- #


def _ldl_evidence() -> TurnEvidence:
    return turn_evidence(pending_bundle(), ("find_pending_document_facts", {"test_query": "ldl"}))


def test_a_labelled_pending_fact_passes_and_is_cited_as_a_pending_fact() -> None:
    stmt = statement("LDL Cholesterol 131 mg/dL on 2026-09-01, read from document 201 page 2, is not yet verified or filed.", "fact", LDL_PENDING)
    verified, code = verify_statement(stmt, _ldl_evidence())
    assert code is None and verified is not None
    assert [c.record_type for c in verified.citations] == [RecordType.PENDING_DOCUMENT_FACT]


def test_a_pending_fact_without_the_label_is_rejected() -> None:
    stmt = statement("LDL Cholesterol was 131 mg/dL on 2026-09-01.", "fact", LDL_PENDING)
    assert verify_statement(stmt, _ldl_evidence()) == (None, "pending_label_missing")


@pytest.mark.parametrize(
    "text",
    [
        "LDL Cholesterol 131 mg/dL is in the chart (not yet verified or filed).",
        "LDL Cholesterol 131 mg/dL is on file, not yet verified or filed.",
        "The filed result for LDL Cholesterol is 131 mg/dL, not yet verified or filed.",
        "LDL Cholesterol 131 mg/dL was added to the patient's record; it is not yet verified or filed.",
    ],
)
def test_a_pending_fact_is_never_presented_as_chart_data(text: str) -> None:
    assert verify_statement(statement(text, "fact", LDL_PENDING), _ldl_evidence()) == (None, "pending_cited_as_chart")


def test_a_derived_flag_on_a_pending_fact_does_not_license_high() -> None:
    stmt = statement("LDL Cholesterol 131 mg/dL is high, not yet verified or filed.", "fact", LDL_PENDING)
    assert verify_statement(stmt, _ldl_evidence()) == (None, "interpretation_without_flag")


def test_no_result_on_file_must_account_for_a_pending_value() -> None:
    bundle = pending_bundle(pending_row(test_name="Potassium", value_text="5.9", unit="mmol/L", reference_range="3.5-5.1", collection_date="2026-09-01"))
    data = bundle.model_dump(mode="json")
    data["lab_results"] = []
    bundle = ContextBundle.model_validate(data)
    ev = turn_evidence(bundle, ("find_results", {"test_query": "potassium"}))
    assert verify_statement(statement("No potassium result was found.", "no_record_found"), ev) == (None, "absence_ignores_pending")
    ok, code = verify_statement(
        statement("No filed potassium result was found; a value read from an uploaded document is not yet verified or filed.", "no_record_found"), ev
    )
    assert code is None and ok is not None and ok.kind is StatementKind.NO_RECORD_FOUND


def test_absence_with_no_pending_value_is_unchanged() -> None:
    ev = turn_evidence(pending_bundle(), ("find_results", {"test_query": "tsh"}))
    verified, code = verify_statement(statement("No TSH result was found.", "no_record_found"), ev)
    assert code is None and verified is not None


def _a1c_conflict_evidence() -> TurnEvidence:
    return turn_evidence(
        pending_bundle(),
        ("find_results", {"test_query": "hba1c"}),
        ("find_pending_document_facts", {"test_query": "hba1c"}),
    )


def test_a_conflicting_pending_value_cannot_be_stated_alone() -> None:
    stmt = statement("Hemoglobin A1c 9.8 % from document 201 is not yet verified or filed.", "fact", A1C_PENDING)
    assert verify_statement(stmt, _a1c_conflict_evidence()) == (None, "conflict_not_shown")


def test_the_filed_value_cannot_be_stated_alone_once_a_conflicting_pending_value_is_known() -> None:
    stmt = statement("The latest Hemoglobin A1c was 8.9 %.", "fact", A1C_CHART)
    assert verify_statement(stmt, _a1c_conflict_evidence()) == (None, "conflict_not_shown")


def test_a_conflict_cited_on_both_sides_and_named_as_a_conflict_passes() -> None:
    stmt = statement(
        "The filed Hemoglobin A1c is 8.9 %; a value of 9.8 % read from document 201, not yet verified or filed, conflicts with it.",
        "fact",
        A1C_CHART,
        A1C_PENDING,
    )
    verified, code = verify_statement(stmt, _a1c_conflict_evidence())
    assert code is None and verified is not None
    assert {c.record_type for c in verified.citations} == {RecordType.LAB_RESULT, RecordType.PENDING_DOCUMENT_FACT}


def test_both_sides_cited_but_not_named_as_a_conflict_is_rejected() -> None:
    stmt = statement("Hemoglobin A1c 8.9 % and 9.8 % (not yet verified or filed).", "fact", A1C_CHART, A1C_PENDING)
    assert verify_statement(stmt, _a1c_conflict_evidence()) == (None, "conflict_not_shown")


def test_the_filed_value_alone_is_fine_when_no_pending_value_was_looked_up() -> None:
    ev = turn_evidence(pending_bundle(), ("find_results", {"test_query": "hba1c"}))
    verified, code = verify_statement(statement("The latest Hemoglobin A1c was 8.9 %.", "fact", A1C_CHART), ev)
    assert code is None and verified is not None


# --------------------------------------------------------------------------- #
# The loop and the prompt
# --------------------------------------------------------------------------- #


class _RecordingProvider(FakeProvider):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tool_names: list[list[str]] = []

    async def turn_step(self, system: str, transcript: list[Any], tools: list[dict[str, Any]], *, force_answer: bool) -> Any:
        self.tool_names.append([t["name"] for t in tools])
        return await super().turn_step(system, transcript, tools, force_answer=force_answer)


@pytest.mark.anyio
async def test_a_follow_up_answers_from_pending_facts_without_re_extraction() -> None:
    bundle = pending_bundle()
    provider = _RecordingProvider(
        turn_script=[
            calls(("find_pending_document_facts", {"test_query": "ldl"})),
            answer(statement("LDL Cholesterol 131 mg/dL on 2026-09-01 is not yet verified or filed.", "fact", LDL_PENDING)),
        ]
    )
    outcome = await run_turn(provider, bundle, [], [], "What did the new lab report say about LDL?")
    assert [s.text for s in outcome.statements] == ["LDL Cholesterol 131 mg/dL on 2026-09-01 is not yet verified or filed."]
    assert provider.calls == []  # no extraction, no structured-output call: the bundle already holds the facts
    assert "find_pending_document_facts" in provider.tool_names[0]


@pytest.mark.anyio
async def test_a_bundle_with_no_pending_facts_offers_the_week_1_tools_only() -> None:
    provider = _RecordingProvider(turn_script=[answer(statement("Which value do you mean?", "clarification"))])
    await run_turn(provider, rich_bundle(), [], [], "A1c?")
    assert "find_pending_document_facts" not in provider.tool_names[0]


def test_the_prompt_teaches_the_pending_rules_the_verifier_enforces() -> None:
    assert "find_pending_document_facts" in FOLLOWUP_SYSTEM_PROMPT
    assert f'"{PENDING_LABEL}"' in FOLLOWUP_SYSTEM_PROMPT
    assert "pending_count" in FOLLOWUP_SYSTEM_PROMPT
    assert "conflicts_with" in FOLLOWUP_SYSTEM_PROMPT and "conflicts with" in FOLLOWUP_SYSTEM_PROMPT
