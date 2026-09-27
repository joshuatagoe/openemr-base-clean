"""Follow-ups apply the briefing's 12-month ageing rule to pending document facts.

A pending value from a document dated more than UNREVIEWED_MAX_AGE_MONTHS
before today is still findable by ``find_pending_document_facts`` (hiding it
would let "no result found" stand beside an unreviewed value), but it is
marked, and any statement citing it must say it comes from an older document
that was never reviewed - besides "not yet verified or filed".

The document's date is the briefing's: its latest collection date among the
values sent, else the date it was received, else it is treated as recent.
One source of truth: the same function the briefing uses.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app import ageing, document_briefing
from app.contracts import ContextBundle, PendingDocumentFact, StatementKind
from app.followup import run_turn
from app.providers.base import ModelStatement
from app.providers.prompt import AGED_LABEL, FOLLOWUP_SYSTEM_PROMPT, PENDING_LABEL
from app.tools import PENDING_TOOL, run_tool
from app.verifier import TurnEvidence, verify_statement
from tests.fakes import FakeProvider, answer, calls, statement
from tests.test_pending_facts import pending_bundle, pending_row

AS_OF = date(2026, 9, 26)
OLD = "copilot_extracted_value:51"
NEW = "copilot_extracted_value:52"


def _bundle(*rows: dict[str, Any]) -> ContextBundle:
    return pending_bundle(*rows)


def _records(bundle: ContextBundle, as_of: date | None = AS_OF) -> dict[str, dict[str, Any]]:
    out = run_tool(bundle, [], PENDING_TOOL, {"test_query": None}, as_of=as_of)
    assert out.error is None
    return {r["record_id"]: r for r in out.records}


def test_one_source_of_truth_for_the_rule() -> None:
    assert document_briefing.is_aged is ageing.is_aged
    assert document_briefing.UNREVIEWED_MAX_AGE_MONTHS == ageing.UNREVIEWED_MAX_AGE_MONTHS == 12


def test_contract_carries_received_at_optionally() -> None:
    fact = PendingDocumentFact.model_validate(pending_row(received_at="2026-09-20"))
    assert fact.received_at == date(2026, 9, 20)
    assert PendingDocumentFact.model_validate(pending_row()).received_at is None


def test_an_old_collection_date_marks_the_value_aged_but_keeps_it_findable() -> None:
    bundle = _bundle(
        pending_row(fact_id=OLD, document_id=301, collection_date="2025-01-10", received_at="2026-09-20"),
        pending_row(fact_id=NEW, document_id=302, collection_date="2026-09-01"),
    )
    records = _records(bundle)
    assert set(records) == {OLD, NEW}, "an aged value stays findable"
    old = records[OLD]
    assert old["aged"] is True
    assert old["document_date"] == "2025-01-10" and old["document_date_kind"] == "collected"
    assert old["age_label"] == f"{AGED_LABEL} (collected 2025-01-10) that was never reviewed"
    assert "aged" not in records[NEW], "a recent value's record is unchanged"


def test_the_document_is_dated_by_its_latest_collection_date() -> None:
    bundle = _bundle(
        pending_row(fact_id=OLD, document_id=301, collection_date="2025-01-10"),
        pending_row(fact_id=NEW, document_id=301, collection_date="2026-08-01"),
    )
    assert all("aged" not in r for r in _records(bundle).values())


def test_no_collection_date_falls_back_to_received_at_and_undated_is_recent() -> None:
    received = _bundle(pending_row(fact_id=OLD, collection_date=None, received_at="2024-02-01"))
    rec = _records(received)[OLD]
    assert rec["aged"] is True and rec["document_date_kind"] == "received"
    undated = _bundle(pending_row(fact_id=OLD, collection_date=None))
    assert "aged" not in _records(undated)[OLD], "what cannot be dated is never hidden or marked"


def test_exactly_twelve_months_is_not_aged() -> None:
    assert "aged" not in _records(_bundle(pending_row(fact_id=OLD, collection_date="2025-09-26")))[OLD]
    assert _records(_bundle(pending_row(fact_id=OLD, collection_date="2025-09-25")))[OLD]["aged"] is True


def test_without_a_date_to_age_against_nothing_is_marked() -> None:
    bundle = _bundle(pending_row(fact_id=OLD, collection_date="2020-01-01"))
    assert "aged" not in _records(bundle, as_of=None)[OLD]


def _verify(bundle: ContextBundle, text: str, *cited: str) -> tuple[Any, str | None]:
    evidence = TurnEvidence([run_tool(bundle, [], PENDING_TOOL, {"test_query": None}, as_of=AS_OF)])
    return verify_statement(ModelStatement(text=text, kind=StatementKind.FACT, citation_record_ids=list(cited)), evidence)


def test_citing_an_aged_value_requires_the_older_document_wording() -> None:
    bundle = _bundle(pending_row(fact_id=OLD, document_id=301, collection_date="2025-01-10"))
    _, code = _verify(bundle, f"Hemoglobin A1c 9.8 % from document 301 is {PENDING_LABEL}.", OLD)
    assert code == "aged_label_missing"
    kept, code = _verify(
        bundle,
        f"Hemoglobin A1c 9.8 % is {AGED_LABEL} (collected 2025-01-10) that was never reviewed; {PENDING_LABEL}.",
        OLD,
    )
    assert code is None and kept is not None


def test_the_pending_label_is_still_required_for_an_aged_value() -> None:
    bundle = _bundle(pending_row(fact_id=OLD, collection_date="2025-01-10"))
    _, code = _verify(bundle, f"Hemoglobin A1c 9.8 % is {AGED_LABEL} (collected 2025-01-10) that was never reviewed.", OLD)
    assert code == "pending_label_missing"


def test_a_recent_value_needs_no_age_wording() -> None:
    bundle = _bundle(pending_row(fact_id=NEW, collection_date="2026-09-01"))
    kept, code = _verify(bundle, f"Hemoglobin A1c 9.8 % from document 201 is {PENDING_LABEL}.", NEW)
    assert code is None and kept is not None


def test_prompt_teaches_the_age_wording() -> None:
    assert AGED_LABEL in FOLLOWUP_SYSTEM_PROMPT and "age_label" in FOLLOWUP_SYSTEM_PROMPT


@pytest.mark.anyio
async def test_run_turn_ages_against_the_given_date() -> None:
    bundle = _bundle(pending_row(fact_id=OLD, collection_date="2025-01-10"))
    provider = FakeProvider(
        turn_script=[
            calls((PENDING_TOOL, {"test_query": "hba1c"})),
            answer(statement(f"Hemoglobin A1c 9.8 % from document 201 is {PENDING_LABEL}.", "fact", OLD)),
        ]
    )
    outcome = await run_turn(provider, bundle, [], [], "a1c?", as_of=AS_OF)
    assert outcome.rejection_codes == ["aged_label_missing"]
