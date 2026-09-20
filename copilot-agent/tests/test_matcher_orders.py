"""Matcher: orders, synonym-table resolution, panels, unresolvable tests and `other` commitments.

Complements test_matcher.py (results-only scenarios). Every case is a
boundary, invariant or regression guard from ARCHITECTURE.md section 9.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.contracts import (
    CommitmentKind,
    ContextBundle,
    EvidenceMatch,
    EvidenceSource,
    EvidenceState,
    ExtractedCommitment,
    LabOrder,
    LabOrderStatus,
    LabResult,
    LabResultStatus,
    RecordType,
)
from app.matcher import match_evidence
from app.synonyms import known_panel_keys, known_test_keys, resolve_commitment, resolve_record
from tests.test_matcher import extraction, load_context, single

NOTE_DATE = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)
AFTER = NOTE_DATE + timedelta(days=60)
LATER = NOTE_DATE + timedelta(days=90)


def commitment(test_name: str | None, kind: CommitmentKind = CommitmentKind.LAB_TEST, span: str = "Repeat labs in three months.") -> ExtractedCommitment:
    return ExtractedCommitment(commitment_id="c-001", kind=kind, source_span=span, test_name=test_name)


def order(order_id: str, test_name: str, *, code: str | None = None, status: LabOrderStatus = LabOrderStatus.PENDING, at: datetime = AFTER, seq: int = 1) -> LabOrder:
    return LabOrder(order_id=order_id, sequence=seq, test_name=test_name, code=code, status=status, ordered_at=at)


def res(result_id: str, test_name: str, value: str, *, code: str | None = None, at: datetime = LATER, order_id: str | None = None, status: LabResultStatus = LabResultStatus.FINAL) -> LabResult:
    return LabResult(result_id=result_id, order_id=order_id, test_name=test_name, code=code, value=Decimal(value), units="mg/dL", status=status, observed_at=at)


def ctx(*, results: list[LabResult] = (), orders: list[LabOrder] = (), unavailable: list[EvidenceSource] = ()) -> ContextBundle:
    base = load_context()
    return base.model_copy(
        update={
            "lab_results": list(results),
            "lab_orders": list(orders),
            "data_quality": base.data_quality.model_copy(update={"sources_unavailable": list(unavailable)}),
        },
        deep=True,
    )


# --------------------------------------------------------------------------- #
# Synonym table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "expected_keys", "panel"),
    [
        ("HbA1c", {"hba1c"}, None),
        ("Hemoglobin A1c", {"hba1c"}, None),
        ("a1c", {"hba1c"}, None),
        ("LDL-C", {"ldl"}, None),
        ("fasting lipid panel", {"ldl", "hdl", "triglycerides", "total_cholesterol"}, "lipid_panel"),
        ("BMP", {"sodium", "potassium", "bun", "creatinine", "glucose", "calcium", "egfr"}, "bmp"),
        ("TSH", {"tsh"}, None),
        ("urine microalbumin", {"urine_acr"}, None),
    ],
)
def test_commitment_names_resolve_through_the_table(name: str, expected_keys: set[str], panel: str | None) -> None:
    """Invariant: aliases and panels resolve to explicit canonical keys; nothing fuzzy."""
    r = resolve_commitment(name)
    assert r is not None
    assert r.keys == frozenset(expected_keys)
    assert r.panel == panel


@pytest.mark.parametrize("name", [None, "", "   ", "labs", "bloodwork", "some novel assay"])
def test_unknown_names_do_not_resolve(name: str | None) -> None:
    assert resolve_commitment(name) is None


def test_records_resolve_by_loinc_before_name() -> None:
    """Invariant: a LOINC code wins over a differently worded name; unknown code falls back to the name; unknown both -> None."""
    assert resolve_record("Glycohemoglobin (weird lab label)", "4548-4") == "hba1c"
    assert resolve_record("Hemoglobin A1c", "LAB-LOCAL-1") == "hba1c"
    assert resolve_record("Mystery test", "LAB-LOCAL-1") is None


def test_table_is_internally_consistent() -> None:
    """Regression guard: every panel member is a known test key."""
    tests = known_test_keys()
    for alias in ("lipid panel", "bmp", "cmp", "lfts", "cbc", "thyroid panel"):
        r = resolve_commitment(alias)
        assert r is not None and r.panel in known_panel_keys()
        assert r.keys <= tests, alias


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #


def test_order_without_result_is_order_found_no_result() -> None:
    """A pending order after the note, no result -> order_found_no_result, citing the order and the note."""
    m = single(ctx(orders=[order("procedure_order:12", "Hemoglobin A1c", code="4548-4")]), extraction(commitment("HbA1c")))
    assert m.state is EvidenceState.ORDER_FOUND_NO_RESULT
    assert [c.record_type for c in m.citations] == [RecordType.PRIOR_NOTE, RecordType.LAB_ORDER]
    assert m.citations[1].record_id == "procedure_order:12:1"
    assert "pending" in m.summary and "no result" in m.summary


def test_canceled_order_is_not_evidence() -> None:
    """Boundary: a canceled order does not count; with nothing else it is no_matching_record_found."""
    m = single(ctx(orders=[order("procedure_order:12", "Hemoglobin A1c", status=LabOrderStatus.CANCELED)]), extraction(commitment("HbA1c")))
    assert m.state is EvidenceState.NO_MATCHING_RECORD_FOUND


def test_order_before_the_note_is_ignored() -> None:
    m = single(ctx(orders=[order("procedure_order:12", "Hemoglobin A1c", at=NOTE_DATE - timedelta(days=1))]), extraction(commitment("HbA1c")))
    assert m.state is EvidenceState.NO_MATCHING_RECORD_FOUND


def test_result_wins_over_order_and_cites_its_order() -> None:
    """Invariant: a completed result outranks an open order; the linked order is cited alongside."""
    m = single(
        ctx(orders=[order("procedure_order:12", "Hemoglobin A1c", status=LabOrderStatus.COMPLETE)], results=[res("procedure_result:1", "Hemoglobin A1c", "7.1", order_id="procedure_order:12")]),
        extraction(commitment("HbA1c")),
    )
    assert m.state is EvidenceState.MATCHING_RESULT_FOUND
    assert [c.record_id for c in m.citations] == ["form_soap:1001", "procedure_result:1", "procedure_order:12:1"]


def test_latest_of_several_orders_is_cited_and_the_rest_are_candidates() -> None:
    m = single(
        ctx(orders=[order("procedure_order:12", "Hemoglobin A1c", at=AFTER), order("procedure_order:13", "HbA1c", at=LATER, status=LabOrderStatus.ROUTED)]),
        extraction(commitment("HbA1c")),
    )
    assert m.state is EvidenceState.ORDER_FOUND_NO_RESULT
    assert m.citations[1].record_id == "procedure_order:13:1"
    assert [c.record_id for c in m.candidates] == ["procedure_order:12:1"]


def test_orders_source_unavailable_with_no_result_is_verification_unavailable() -> None:
    """Missing/conflicting class: cannot tell 'no order' from 'orders could not be read'."""
    m = single(ctx(unavailable=[EvidenceSource.LAB_ORDERS]), extraction(commitment("HbA1c")))
    assert m.state is EvidenceState.VERIFICATION_UNAVAILABLE
    assert "orders source" in m.summary


def test_orders_source_unavailable_does_not_block_a_found_result() -> None:
    m = single(ctx(unavailable=[EvidenceSource.LAB_ORDERS], results=[res("procedure_result:1", "Hemoglobin A1c", "7.1")]), extraction(commitment("HbA1c")))
    assert m.state is EvidenceState.MATCHING_RESULT_FOUND


# --------------------------------------------------------------------------- #
# Ambiguity and unknown tests
# --------------------------------------------------------------------------- #


def test_unresolvable_test_name_is_ambiguous_not_absent() -> None:
    """Boundary: a test the table does not know cannot be searched for; absence is never asserted."""
    m = single(ctx(results=[res("procedure_result:1", "Hemoglobin A1c", "7.1")]), extraction(commitment("serum rhubarb level")))
    assert m.state is EvidenceState.AMBIGUOUS_MATCH
    assert "curated" in m.summary
    assert [c.record_type for c in m.citations] == [RecordType.PRIOR_NOTE]


def test_unknown_name_with_an_exact_record_name_still_matches() -> None:
    """An exact normalized-name match is safe even for a test outside the table (no fuzzing)."""
    m = single(ctx(results=[res("procedure_result:1", "Serum Rhubarb Level", "3")]), extraction(commitment("serum rhubarb level")))
    assert m.state is EvidenceState.MATCHING_RESULT_FOUND


def test_loinc_coded_result_matches_a_differently_worded_commitment() -> None:
    m = single(ctx(results=[res("procedure_result:1", "GLYCOHEMOGLOBIN A1C", "7.1", code="4548-4")]), extraction(commitment("a1c")))
    assert m.state is EvidenceState.MATCHING_RESULT_FOUND


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #


def test_panel_commitment_matches_member_results_and_cites_each() -> None:
    m = single(
        ctx(results=[res("procedure_result:1", "LDL Cholesterol", "110", code="13457-7"), res("procedure_result:2", "Triglycerides", "150")]),
        extraction(commitment("lipid panel")),
    )
    assert m.state is EvidenceState.MATCHING_RESULT_FOUND
    assert {c.record_id for c in m.citations} == {"form_soap:1001", "procedure_result:1", "procedure_result:2"}
    assert "2 of 4" in m.summary and "LDL" in m.summary


def test_panel_order_without_results_is_order_found_no_result() -> None:
    """An order placed under the panel's own name satisfies a panel commitment (no member results yet)."""
    m = single(ctx(orders=[order("procedure_order:5", "Lipid Panel", code="24331-1")]), extraction(commitment("lipids")))
    assert m.state is EvidenceState.ORDER_FOUND_NO_RESULT
    assert m.citations[1].record_id == "procedure_order:5:1"


def test_member_order_satisfies_a_panel_commitment() -> None:
    m = single(ctx(orders=[order("procedure_order:6", "LDL Cholesterol", code="13457-7")]), extraction(commitment("lipid panel")))
    assert m.state is EvidenceState.ORDER_FOUND_NO_RESULT


def test_panel_member_conflict_is_conflicting_records() -> None:
    m = single(
        ctx(results=[res("procedure_result:1", "LDL Cholesterol", "110", at=LATER), res("procedure_result:2", "LDL Cholesterol", "140", at=LATER)]),
        extraction(commitment("lipid panel")),
    )
    assert m.state is EvidenceState.CONFLICTING_RECORDS
    assert {c.record_id for c in m.citations} == {"form_soap:1001", "procedure_result:1", "procedure_result:2"}


# --------------------------------------------------------------------------- #
# `other` commitments
# --------------------------------------------------------------------------- #


def test_other_commitment_has_no_state_and_is_never_checked() -> None:
    m = single(ctx(results=[res("procedure_result:1", "Hemoglobin A1c", "7.1")]), extraction(commitment(None, kind=CommitmentKind.OTHER, span="Refer to cardiology.")))
    assert m.state is None
    assert "not checked" in m.summary
    assert [c.record_type for c in m.citations] == [RecordType.PRIOR_NOTE]


def test_state_none_is_only_valid_for_other() -> None:
    """Invariant: every checked kind has a state; `other` has none. The contract enforces both directions."""
    lab = commitment("HbA1c")
    other = commitment(None, kind=CommitmentKind.OTHER, span="Refer to cardiology.")
    with pytest.raises(ValueError):
        EvidenceMatch(commitment=lab, state=None, summary="x")
    with pytest.raises(ValueError):
        EvidenceMatch(commitment=other, state=EvidenceState.NO_MATCHING_RECORD_FOUND, summary="x")
    assert EvidenceMatch(commitment=other, state=None, summary="x").state is None


def test_every_commitment_gets_exactly_one_match_in_order() -> None:
    ext = extraction(commitment("HbA1c"), commitment(None, kind=CommitmentKind.OTHER, span="Refer to cardiology."), commitment("TSH"))
    matches = match_evidence(ctx(), ext)
    assert [m.commitment.commitment_id for m in matches] == ["c-001", "c-001", "c-001"]
    assert [m.state for m in matches] == [EvidenceState.NO_MATCHING_RECORD_FOUND, None, EvidenceState.NO_MATCHING_RECORD_FOUND]


def test_panel_order_arriving_once_per_report_is_still_cited_with_its_corrected_result() -> None:
    """Regression (Henry Walsh demo patient): a BMP order with a final and a corrected report is read once per
    report; the two identical order rows must count as one order so the result's order is cited rather than
    the order being left 'unexplained' in the interval list."""
    bmp = [order("procedure_order:7", "Basic Metabolic Panel", code="24320-4", status=LabOrderStatus.COMPLETE, at=AFTER) for _ in range(2)]
    results = [
        res("procedure_result:5", "Sodium", "142", code="2951-2", order_id="procedure_order:7", at=AFTER, status=LabResultStatus.FINAL),
        res("procedure_result:6", "Sodium", "138", code="2951-2", order_id="procedure_order:7", at=LATER, status=LabResultStatus.CORRECTED),
    ]
    m = single(ctx(orders=bmp, results=results), extraction(commitment("basic metabolic panel")))
    assert m.state is EvidenceState.MATCHING_RESULT_FOUND
    assert "procedure_order:7:1" in [c.record_id for c in m.citations]
