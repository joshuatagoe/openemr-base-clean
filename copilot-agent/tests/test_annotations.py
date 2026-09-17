"""Interval annotations: every record after the note gets exactly one annotation; unexplained is null, never dropped."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.annotations import annotate_interval, interval_records
from app.contracts import (
    CommitmentKind,
    ExtractedCommitment,
    LabOrder,
    LabOrderStatus,
    LabResult,
    LabResultStatus,
    MedicationAction,
    MedicationRecord,
    MedicationSource,
    RecordType,
)
from app.matcher import match_evidence
from app.service import BriefingService
from tests.test_matcher import extraction, load_context

NOTE = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)
AFTER = NOTE + timedelta(days=30)
BEFORE = NOTE - timedelta(days=30)


def bundle():
    base = load_context()
    results = [
        LabResult(result_id="procedure_result:9001", test_name="Hemoglobin A1c", value=Decimal("8.9"), units="%", status=LabResultStatus.FINAL, observed_at=AFTER),
        LabResult(result_id="procedure_result:9002", test_name="TSH", value=Decimal("2.1"), units="mIU/L", status=LabResultStatus.FINAL, observed_at=AFTER + timedelta(days=1)),
        LabResult(result_id="procedure_result:9000", test_name="Hemoglobin A1c", value=Decimal("8.1"), units="%", status=LabResultStatus.FINAL, observed_at=BEFORE),
    ]
    orders = [LabOrder(order_id="procedure_order:5", sequence=1, test_name="Lipid Panel", status=LabOrderStatus.PENDING, ordered_at=AFTER)]
    meds = [
        MedicationRecord(record_id="prescriptions:31", source_table=MedicationSource.PRESCRIPTIONS, drug_name="Metformin 500 mg", active=True, status_field="active,end_date", status_value="active=1,end_date=null", started_at=BEFORE, timestamp=BEFORE, timestamp_field="date_added"),
        MedicationRecord(record_id="prescriptions:50", source_table=MedicationSource.PRESCRIPTIONS, drug_name="Atorvastatin 20 mg", active=True, status_field="active,end_date", status_value="active=1,end_date=null", started_at=AFTER, timestamp=AFTER, timestamp_field="date_added"),
    ]
    return base.model_copy(update={"lab_results": results, "lab_orders": orders, "medications": meds}, deep=True)


def test_interval_records_are_only_those_after_the_note_in_stable_order() -> None:
    ids = [rid for rid, _ in interval_records(bundle())]
    assert ids == ["procedure_result:9001", "procedure_result:9002", "procedure_order:5:1", "prescriptions:50"]


def test_annotations_cover_every_interval_record_and_point_at_the_citing_commitment() -> None:
    ctx = bundle()
    ext = extraction(
        ExtractedCommitment(commitment_id="c-001", kind=CommitmentKind.MEDICATION, source_span="Continue metformin.", drug_name="metformin", action=MedicationAction.CONTINUE),
        ExtractedCommitment(commitment_id="c-002", kind=CommitmentKind.LAB_TEST, source_span="Repeat HbA1c in three months.", test_name="HbA1c"),
    )
    matches = match_evidence(ctx, ext)
    annotations = annotate_interval(ctx, matches)
    by_id = {a.record_id: a for a in annotations}
    assert set(by_id) == {"procedure_result:9001", "procedure_result:9002", "procedure_order:5:1", "prescriptions:50"}
    assert by_id["procedure_result:9001"].explained_by == "c-002"
    assert by_id["procedure_result:9001"].record_type is RecordType.LAB_RESULT
    assert by_id["procedure_result:9002"].explained_by is None  # TSH: nothing in the plan
    assert by_id["procedure_order:5:1"].explained_by is None  # lipid order: nothing in the plan
    assert by_id["prescriptions:50"].explained_by is None  # atorvastatin start: nothing in the plan
    # The pre-baseline metformin record is cited by c-001 but is not an interval record.
    assert "prescriptions:31" not in by_id


def test_candidates_also_explain_records() -> None:
    ctx = bundle()
    ext = extraction(ExtractedCommitment(commitment_id="c-001", kind=CommitmentKind.MEDICATION, source_span="Increase atorvastatin.", drug_name="atorvastatin", action=MedicationAction.INCREASE))
    annotations = annotate_interval(ctx, match_evidence(ctx, ext))
    assert next(a for a in annotations if a.record_id == "prescriptions:50").explained_by == "c-001"


def test_no_commitments_means_everything_is_unexplained_but_still_listed() -> None:
    ctx = bundle()
    annotations = annotate_interval(ctx, [])
    assert len(annotations) == 4 and all(a.explained_by is None for a in annotations)


def test_assemble_without_extraction_still_annotates_and_counts_nothing() -> None:
    ctx = bundle()
    resp = BriefingService.assemble(ctx, None, ["No usable prior plan text was found in the supplied note; no commitments were evaluated."])
    assert resp.matches == [] and len(resp.interval_annotations) == 4 and resp.rejected_count == 0
