"""Medication matcher: direction rules from record fields, two-source disagreement, indeterminate status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.contracts import (
    CommitmentKind,
    ContextBundle,
    EvidenceSource,
    EvidenceState,
    ExtractedCommitment,
    MedicationAction,
    MedicationRecord,
    MedicationSource,
    RecordType,
)
from app.matcher import match_evidence
from app.medications import ingredient_key
from tests.test_matcher import extraction, load_context, single

NOTE = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)
BEFORE = NOTE - timedelta(days=100)
AFTER = NOTE + timedelta(days=5)


def med(rid: str, name: str, *, source: MedicationSource = MedicationSource.PRESCRIPTIONS, active: bool | None = True, started: datetime | None = BEFORE, ended: datetime | None = None, modified: datetime | None = None, ts: datetime | None = None) -> MedicationRecord:
    return MedicationRecord(
        record_id=rid, source_table=source, drug_name=name, active=active,
        status_field="active,end_date", status_value=f"active={active},end_date={ended}",
        started_at=started, ended_at=ended, modified_at=modified, timestamp=ts or modified or started or BEFORE, timestamp_field="date_added",
    )


def commitment(drug: str | None, action: MedicationAction | None, span: str = "Continue metformin.") -> ExtractedCommitment:
    return ExtractedCommitment(commitment_id="c-1", kind=CommitmentKind.MEDICATION, source_span=span, drug_name=drug, action=action)


def ctx(*meds: MedicationRecord, unavailable: list[EvidenceSource] = ()) -> ContextBundle:
    base = load_context()
    return base.model_copy(update={"medications": list(meds), "data_quality": base.data_quality.model_copy(update={"sources_unavailable": list(unavailable)})}, deep=True)


@pytest.mark.parametrize(("name", "key"), [("Metformin HCl 500 mg", "metformin"), ("  LISINOPRIL-hctz 10/12.5", "lisinopril"), ("", ""), ("500 mg", "500")])
def test_ingredient_key_is_first_word(name: str, key: str) -> None:
    assert ingredient_key(name) == key


def test_continue_with_active_record_is_found_citing_the_latest() -> None:
    m = single(ctx(med("prescriptions:1", "Metformin 500 mg", ts=BEFORE), med("prescriptions:2", "Metformin 1000 mg", ts=AFTER)), extraction(commitment("metformin", MedicationAction.CONTINUE)))
    assert m.state is EvidenceState.MATCHING_MEDICATION_RECORD_FOUND
    assert [c.record_id for c in m.citations] == ["form_soap:1001", "prescriptions:2"]
    assert [c.record_id for c in m.candidates] == ["prescriptions:1"]
    assert m.citations[1].record_type is RecordType.MEDICATION


def test_continue_with_only_inactive_records_conflicts() -> None:
    m = single(ctx(med("lists:3", "Metformin", source=MedicationSource.LISTS, active=False, ended=BEFORE)), extraction(commitment("metformin", MedicationAction.CONTINUE)))
    assert m.state is EvidenceState.CONFLICTING_RECORDS
    assert "lists:3" in {c.record_id for c in m.citations}


def test_continue_with_indeterminate_status_is_ambiguous() -> None:
    m = single(ctx(med("prescriptions:1", "Metformin", active=None)), extraction(commitment("metformin", MedicationAction.CONTINUE)))
    assert m.state is EvidenceState.AMBIGUOUS_MATCH
    assert [c.record_id for c in m.candidates] == ["prescriptions:1"]


def test_start_requires_a_start_after_the_note() -> None:
    older = ctx(med("prescriptions:1", "Atorvastatin 20 mg", started=BEFORE))
    m = single(older, extraction(commitment("atorvastatin", MedicationAction.START, "Start atorvastatin.")))
    assert m.state is EvidenceState.AMBIGUOUS_MATCH and [c.record_id for c in m.candidates] == ["prescriptions:1"]
    newer = ctx(med("prescriptions:2", "Atorvastatin 20 mg", started=AFTER))
    m = single(newer, extraction(commitment("atorvastatin", MedicationAction.START, "Start atorvastatin.")))
    assert m.state is EvidenceState.MATCHING_MEDICATION_RECORD_FOUND and "2026-06-15" in m.summary


def test_stop_verified_by_deactivation_after_note() -> None:
    m = single(ctx(med("prescriptions:1", "Lisinopril", active=False, modified=AFTER)), extraction(commitment("lisinopril", MedicationAction.STOP, "Stop lisinopril.")))
    assert m.state is EvidenceState.MATCHING_MEDICATION_RECORD_FOUND


def test_stop_with_deactivation_before_note_is_ambiguous() -> None:
    m = single(ctx(med("prescriptions:1", "Lisinopril", active=False, ended=BEFORE, modified=BEFORE)), extraction(commitment("lisinopril", MedicationAction.STOP, "Stop lisinopril.")))
    assert m.state is EvidenceState.AMBIGUOUS_MATCH


def test_two_sources_disagreeing_is_conflicting_regardless_of_action() -> None:
    both = ctx(med("prescriptions:1", "Metformin", active=True), med("lists:2", "Metformin", source=MedicationSource.LISTS, active=False, ended=BEFORE))
    for action in MedicationAction:
        m = single(both, extraction(commitment("metformin", action)))
        assert m.state is EvidenceState.CONFLICTING_RECORDS, action
        assert {c.record_id for c in m.citations} == {"form_soap:1001", "prescriptions:1", "lists:2"}


def test_same_source_active_and_inactive_is_not_a_cross_source_conflict() -> None:
    """Two prescriptions rows (an old ended one and a current one) are normal history, not disagreement."""
    m = single(ctx(med("prescriptions:1", "Metformin 500 mg", active=False, ended=BEFORE), med("prescriptions:2", "Metformin 1000 mg", active=True, ts=AFTER)), extraction(commitment("metformin", MedicationAction.CONTINUE)))
    assert m.state is EvidenceState.MATCHING_MEDICATION_RECORD_FOUND


def test_unrelated_drug_is_not_a_candidate() -> None:
    m = single(ctx(med("prescriptions:1", "Metoprolol 25 mg")), extraction(commitment("metformin", MedicationAction.CONTINUE)))
    assert m.state is EvidenceState.NO_MATCHING_RECORD_FOUND


def test_no_drug_name_is_ambiguous() -> None:
    m = single(ctx(med("prescriptions:1", "Metformin")), extraction(commitment(None, MedicationAction.CONTINUE)))
    assert m.state is EvidenceState.AMBIGUOUS_MATCH


def test_source_unavailable_beats_everything() -> None:
    m = single(ctx(med("prescriptions:1", "Metformin"), unavailable=[EvidenceSource.MEDICATIONS]), extraction(commitment("metformin", MedicationAction.CONTINUE)))
    assert m.state is EvidenceState.VERIFICATION_UNAVAILABLE


def test_dose_change_and_unclear_are_ambiguous_with_candidates() -> None:
    for action in (MedicationAction.INCREASE, MedicationAction.DECREASE, MedicationAction.SWITCH, MedicationAction.UNCLEAR, None):
        m = single(ctx(med("prescriptions:1", "Metformin")), extraction(commitment("metformin", action)))
        assert m.state is EvidenceState.AMBIGUOUS_MATCH, action
        assert [c.record_id for c in m.candidates] == ["prescriptions:1"]
        assert [c.record_type for c in m.citations] == [RecordType.PRIOR_NOTE]


def test_summaries_never_use_negation_or_recommendation_language() -> None:
    contexts = [ctx(), ctx(med("prescriptions:1", "Metformin")), ctx(med("lists:2", "Metformin", source=MedicationSource.LISTS, active=False, ended=BEFORE))]
    for c in contexts:
        for action in MedicationAction:
            for m in match_evidence(c, extraction(commitment("metformin", action))):
                low = m.summary.lower()
                for forbidden in ("not done", "not taking", "should", "recommend", "never"):
                    assert forbidden not in low, (action, m.summary)
