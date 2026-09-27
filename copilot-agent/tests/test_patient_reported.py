"""Intake answers for follow-ups: patient-reported facts (contract C5 ``kind``).

A waiting intake item (chief concern, medication, allergy, family history, or
a section's written "none") reaches the follow-up bundle as a pending fact of
kind ``patient_reported``. It is what the patient wrote, not a lab value and
not chart data:

- it is found only through its own tool, ``find_patient_reported``, labelled;
- every statement citing one must say it is patient-reported (from the intake
  form) and not in the chart; it may never be called a result, lab value or
  chart entry;
- a reported medication that differs from the chart may be stated as a
  discrepancy citing both records;
- the 12-month ageing rule applies (an intake form is dated by its upload date);
- it never carries units, range, flag or collection date, and never a
  demographic field.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from app.contracts import ContextBundle, PendingDocumentFact, RecordType, StatementKind
from app.followup import run_turn
from app.providers.base import ModelStatement
from app.providers.prompt import AGED_LABEL, FOLLOWUP_SYSTEM_PROMPT, PATIENT_REPORTED_LABEL, PENDING_LABEL
from app.tools import PATIENT_REPORTED_TOOL, PENDING_TOOL, run_tool, tool_definitions
from app.verifier import TurnEvidence, verify_statement
from tests.fakes import FakeProvider, answer, calls, statement
from tests.test_followup import rich_bundle
from tests.test_pending_facts import A1C_PENDING, pending_row

MED = "copilot_extracted_value:81"
ALLERGY = "copilot_extracted_value:82"
CONCERN = "copilot_extracted_value:83"
AS_OF = date(2026, 9, 26)


def reported(fact_id: str = MED, label: str = "Current medication", value: str | None = "Metformin 1000 mg twice daily", **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "fact_id": fact_id,
        "document_id": 301,
        "test_name": label,
        "value_text": value,
        "unit": None,
        "reference_range": None,
        "abnormal_flag": None,
        "flag_source": "unavailable",
        "collection_date": None,
        "verification_status": "verified_exact",
        "page": 1,
        "bbox": None,
        "status": "candidate",
        "kind": "patient_reported",
        "received_at": "2026-09-25",
    }
    row.update(over)
    return row


def bundle(*rows: dict[str, Any]) -> ContextBundle:
    data = rich_bundle().model_dump(mode="json")
    data["pending_document_facts"] = list(rows) or [
        reported(),
        reported(ALLERGY, "Allergy", "Penicillin; reaction: rash"),
        reported(CONCERN, "Chief concern", "High blood pressure readings at home"),
        pending_row(),
    ]
    return ContextBundle.model_validate(data)


def evidence(b: ContextBundle, *tool_calls: tuple[str, dict[str, Any]]) -> TurnEvidence:
    return TurnEvidence([run_tool(b, [], name, args, as_of=AS_OF) for name, args in tool_calls])


def verify(b: ContextBundle, text: str, *cited: str, tools: tuple[tuple[str, dict[str, Any]], ...] = ((PATIENT_REPORTED_TOOL, {"query": None}),)) -> tuple[Any, str | None]:
    return verify_statement(ModelStatement(text=text, kind=StatementKind.FACT, citation_record_ids=list(cited)), evidence(b, *tools))


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #


def test_kind_defaults_to_a_lab_value_and_patient_reported_carries_no_lab_fields() -> None:
    assert PendingDocumentFact.model_validate(pending_row()).kind == "lab_value"
    assert PendingDocumentFact.model_validate(reported()).kind == "patient_reported"
    for field, value in (("unit", "mg"), ("reference_range", "0-1"), ("abnormal_flag", "H"), ("collection_date", "2026-09-01"), ("flag_source", "extracted")):
        with pytest.raises(ValidationError):
            PendingDocumentFact.model_validate(reported(**{field: value}))


def test_only_intake_item_labels_are_accepted_never_a_demographic() -> None:
    for label in ("Patient name", "Date of birth", "Hemoglobin A1c"):
        with pytest.raises(ValidationError):
            PendingDocumentFact.model_validate(reported(label=label, value="Whitfield, Evelyn R."))


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


def test_reported_items_have_their_own_labelled_tool_and_never_appear_as_lab_values() -> None:
    b = bundle()
    out = run_tool(b, [], PATIENT_REPORTED_TOOL, {"query": "metformin"}, as_of=AS_OF)
    assert out.error is None
    assert [r["record_id"] for r in out.records] == [MED]
    rec = out.records[0]
    assert rec["label"] == PATIENT_REPORTED_LABEL and rec["source"] == "intake form" and rec["kind"] == "patient_reported"
    assert rec["item"] == "Current medication" and rec["value"] == "Metformin 1000 mg twice daily"
    assert "units" not in rec and "abnormal_flag" not in rec and "range" not in rec

    everything = run_tool(b, [], PATIENT_REPORTED_TOOL, {"query": None}, as_of=AS_OF)
    assert {r["record_id"] for r in everything.records} == {MED, ALLERGY, CONCERN}

    pending = run_tool(b, [], PENDING_TOOL, {"test_query": None}, as_of=AS_OF)
    assert [r["record_id"] for r in pending.records] == [A1C_PENDING], "intake items are not pending lab values"
    results = run_tool(b, [], "find_results", {"test_query": "hba1c"}, as_of=AS_OF)
    assert results.pending_count == 1


def test_the_tool_is_offered_only_when_there_are_reported_items() -> None:
    names = {d["name"] for d in tool_definitions(include_pending=True, include_patient_reported=True)}
    assert PATIENT_REPORTED_TOOL in names
    assert PATIENT_REPORTED_TOOL not in {d["name"] for d in tool_definitions(include_pending=True)}


def test_an_old_intake_form_is_marked_aged() -> None:
    b = bundle(reported(received_at="2024-01-10"))
    rec = run_tool(b, [], PATIENT_REPORTED_TOOL, {"query": None}, as_of=AS_OF).records[0]
    assert rec["aged"] is True and rec["document_date_kind"] == "received"


# --------------------------------------------------------------------------- #
# Verifier
# --------------------------------------------------------------------------- #


def test_a_statement_citing_an_item_must_say_patient_reported_and_not_in_the_chart() -> None:
    b = bundle()
    _, code = verify(b, "The patient takes Metformin 1000 mg twice daily.", MED)
    assert code == "patient_reported_label_missing"
    _, code = verify(b, f"Metformin 1000 mg twice daily is {PENDING_LABEL}.", MED)
    assert code == "patient_reported_label_missing", "the lab label is not the patient-reported one"
    kept, code = verify(b, f"Current medication: Metformin 1000 mg twice daily ({PATIENT_REPORTED_LABEL}).", MED)
    assert code is None and kept is not None
    assert kept.citations[0].record_type is RecordType.PATIENT_REPORTED


def test_an_item_is_never_called_a_result_or_chart_data() -> None:
    b = bundle()
    _, code = verify(b, f"The allergy result on file is Penicillin; reaction: rash ({PATIENT_REPORTED_LABEL}).", ALLERGY)
    assert code in {"patient_reported_as_chart", "patient_reported_as_lab"}
    _, code = verify(b, f"The lab value reported is Metformin 1000 mg ({PATIENT_REPORTED_LABEL}).", MED)
    assert code == "patient_reported_as_lab"


def test_a_reported_medication_that_differs_from_the_chart_may_be_a_discrepancy_citing_both() -> None:
    b = bundle()
    chart_med = next(m for m in b.medications if "metformin" in m.drug_name.lower())
    tools = ((PATIENT_REPORTED_TOOL, {"query": "metformin"}), ("find_medications", {"drug_query": "metformin"}))
    text = f"The chart lists {chart_med.drug_name}; the intake form reports Metformin 1000 mg twice daily ({PATIENT_REPORTED_LABEL}), a discrepancy."
    kept, code = verify(b, text, MED, chart_med.record_id, tools=tools)
    assert code is None, code
    assert {c.record_type for c in kept.citations} == {RecordType.PATIENT_REPORTED, RecordType.MEDICATION}


def test_the_patients_own_words_may_be_quoted() -> None:
    b = bundle()
    kept, code = verify(b, f"Chief concern: High blood pressure readings at home ({PATIENT_REPORTED_LABEL}).", CONCERN)
    assert code is None and kept is not None
    _, code = verify(b, f"Penicillin is a high-risk allergy ({PATIENT_REPORTED_LABEL}).", ALLERGY)
    assert code == "interpretation_without_flag", "only words the patient wrote are quoted"


def test_an_aged_item_needs_the_older_document_wording_too() -> None:
    b = bundle(reported(received_at="2024-01-10"))
    _, code = verify(b, f"Metformin 1000 mg twice daily ({PATIENT_REPORTED_LABEL}).", MED)
    assert code == "aged_label_missing"
    kept, code = verify(b, f"Metformin 1000 mg twice daily ({PATIENT_REPORTED_LABEL}), {AGED_LABEL} (received 2024-01-10) that was never reviewed.", MED)
    assert code is None and kept is not None


def test_prompt_teaches_the_label_and_the_tool() -> None:
    assert PATIENT_REPORTED_LABEL in FOLLOWUP_SYSTEM_PROMPT and PATIENT_REPORTED_TOOL in FOLLOWUP_SYSTEM_PROMPT
    assert "discrepancy" in FOLLOWUP_SYSTEM_PROMPT


@pytest.mark.anyio
async def test_run_turn_offers_the_tool_and_verifies_the_label() -> None:
    b = bundle(reported())
    provider = FakeProvider(
        turn_script=[
            calls((PATIENT_REPORTED_TOOL, {"query": "metformin"})),
            answer(
                statement("The patient takes Metformin 1000 mg twice daily.", "fact", MED),
                statement(f"Metformin 1000 mg twice daily ({PATIENT_REPORTED_LABEL}).", "fact", MED),
            ),
        ]
    )
    outcome = await run_turn(provider, b, [], [], "What meds did she list?", as_of=AS_OF)
    assert outcome.rejection_codes == ["patient_reported_label_missing"]
    assert [s.citations[0].record_type for s in outcome.statements] == [RecordType.PATIENT_REPORTED]


def test_the_contract_labels_are_the_intake_item_labels() -> None:
    from app import intake
    from app.contracts import PATIENT_REPORTED_LABELS

    assert PATIENT_REPORTED_LABELS == {getattr(intake, n) for n in dir(intake) if n.startswith("LABEL_")}
