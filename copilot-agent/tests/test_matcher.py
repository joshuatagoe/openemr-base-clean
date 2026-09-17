"""Tests for the deterministic evidence matcher.

Commitments are constructed directly; no note-language parsing is exercised
here (that belongs to the future extractor). Each test names the failure mode
it guards against.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.contracts import (
    AbnormalFlag,
    BriefingRequest,
    CommitmentKind,
    ContextBundle,
    DataQuality,
    EvidenceSource,
    EvidenceState,
    ExtractedCommitment,
    ExtractionOutput,
    LabResult,
    LabResultStatus,
    RecordType,
)
from app.matcher import match_evidence

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "lab_followup.json"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def load_context() -> ContextBundle:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return BriefingRequest.model_validate(json.load(fh)).context


def hba1c_commitment(test_name: str | None = "HbA1c") -> ExtractedCommitment:
    return ExtractedCommitment(
        commitment_id="commitment-001",
        kind=CommitmentKind.LAB_TEST,
        source_span="Repeat HbA1c in three months.",
        test_name=test_name,
        due_text="in three months",
    )


def extraction(*commitments: ExtractedCommitment) -> ExtractionOutput:
    return ExtractionOutput(commitments=list(commitments), warnings=[])


def result(
    result_id: str,
    *,
    test_name: str = "Hemoglobin A1c",
    value: str = "8.9",
    units: str | None = "%",
    status: LabResultStatus = LabResultStatus.FINAL,
    observed_at: datetime,
    abnormal_flag: AbnormalFlag | None = None,
) -> LabResult:
    return LabResult(
        result_id=result_id,
        test_name=test_name,
        value=Decimal(value),
        units=units,
        status=status,
        observed_at=observed_at,
        abnormal_flag=abnormal_flag,
    )


def with_results(context: ContextBundle, results: list[LabResult]) -> ContextBundle:
    return context.model_copy(update={"lab_results": results}, deep=True)


def single(context: ContextBundle, ext: ExtractionOutput):
    matches = match_evidence(context, ext)
    assert len(matches) == 1
    return matches[0]


NOTE_DATE = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)
AFTER = NOTE_DATE + timedelta(days=90)


# --------------------------------------------------------------------------- #
# 1. Tracer bullet
# --------------------------------------------------------------------------- #


def test_fixture_produces_matching_result_found() -> None:
    """Tracer bullet: fixture note + final high HbA1c -> matching_result_found with source citations."""
    context = load_context()
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.MATCHING_RESULT_FOUND
    cited = {(c.record_type, c.record_id) for c in match.citations}
    assert cited == {
        (RecordType.PRIOR_NOTE, "form_soap:1001"),
        (RecordType.LAB_RESULT, "procedure_result:9001"),
    }
    assert "8.9%" in match.summary
    assert "high" in match.summary


# --------------------------------------------------------------------------- #
# 2-3. Canonicalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "commitment_name, result_name",
    [
        ("HbA1c", "Hemoglobin A1c"),
        ("Hemoglobin A1c", "HbA1c"),
        ("A1c", "Glycated hemoglobin"),
        ("Glycated hemoglobin", "HbA1c"),
    ],
)
def test_aliases_canonicalize_to_the_same_test(commitment_name: str, result_name: str) -> None:
    """Guards: the explicit alias map is symmetric - any alias on either side matches (via public behaviour only)."""
    context = with_results(load_context(), [result("procedure_result:1", test_name=result_name, observed_at=AFTER)])
    match = single(context, extraction(hba1c_commitment(commitment_name)))
    assert match.state is EvidenceState.MATCHING_RESULT_FOUND
    assert any(c.record_id == "procedure_result:1" for c in match.citations)


def test_matching_is_case_and_whitespace_insensitive() -> None:
    """Guards: casing, surrounding whitespace and harmless punctuation must not defeat a match."""
    context = with_results(
        load_context(), [result("procedure_result:1", test_name="  HEMOGLOBIN   A1C. ", observed_at=AFTER)]
    )
    match = single(context, extraction(hba1c_commitment("hba1c")))
    assert match.state is EvidenceState.MATCHING_RESULT_FOUND


def test_unknown_names_match_only_identical_normalized_names() -> None:
    """Guards: no fuzzy or substring matching - a result named 'Hemoglobin' is not evidence for HbA1c."""
    context = with_results(
        load_context(), [result("procedure_result:hb", test_name="Hemoglobin", value="13.2", units="g/dL", observed_at=AFTER)]
    )
    match = single(context, extraction(hba1c_commitment("HbA1c")))
    assert match.state is EvidenceState.NO_MATCHING_RECORD_FOUND
    # And an unlisted name still matches its own exact normalized form.
    context = with_results(
        load_context(), [result("procedure_result:k", test_name="Potassium", value="4.1", units="mmol/L", observed_at=AFTER)]
    )
    match = single(context, extraction(hba1c_commitment("POTASSIUM ")))
    assert match.state is EvidenceState.MATCHING_RESULT_FOUND


# --------------------------------------------------------------------------- #
# 4-7. Window, relatedness, absence, status
# --------------------------------------------------------------------------- #


def test_result_before_prior_note_is_ignored() -> None:
    """Boundary: evidence must postdate the plan; an older result cannot satisfy a forward-looking commitment."""
    context = with_results(load_context(), [result("procedure_result:old", observed_at=NOTE_DATE - timedelta(days=1))])
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.NO_MATCHING_RECORD_FOUND
    assert [c.record_type for c in match.citations] == [RecordType.PRIOR_NOTE]


def test_result_at_exactly_note_time_is_ignored() -> None:
    """Boundary: the window is strictly after the note timestamp."""
    context = with_results(load_context(), [result("procedure_result:same", observed_at=NOTE_DATE)])
    assert single(context, extraction(hba1c_commitment())).state is EvidenceState.NO_MATCHING_RECORD_FOUND


def test_unrelated_subsequent_test_is_not_matched() -> None:
    """Guards: a different test after the note (here potassium) is not evidence for an HbA1c commitment."""
    context = with_results(
        load_context(),
        [result("procedure_result:k", test_name="Potassium", value="4.1", units="mmol/L", observed_at=AFTER)],
    )
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.NO_MATCHING_RECORD_FOUND
    assert all(c.record_id != "procedure_result:k" for c in match.citations)


def test_no_subsequent_matching_result_returns_no_matching_record_found() -> None:
    """Boundary: empty evidence -> scoped absence claim citing only the note; never a 'not done' claim."""
    context = with_results(load_context(), [])
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.NO_MATCHING_RECORD_FOUND
    assert "supplied records" in match.summary
    assert "not performed" not in match.summary and "not done" not in match.summary


def test_preliminary_only_result_returns_verification_unavailable() -> None:
    """Guards: a preliminary result is not completed evidence, but it is also not absence - it is cited."""
    context = with_results(
        load_context(),
        [result("procedure_result:prelim", status=LabResultStatus.PRELIMINARY, observed_at=AFTER)],
    )
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.VERIFICATION_UNAVAILABLE
    assert {c.record_id for c in match.citations} == {"form_soap:1001", "procedure_result:prelim"}


def test_corrected_result_is_eligible() -> None:
    """Guards: a corrected result counts as completed evidence (AUDIT DATA-005)."""
    context = with_results(
        load_context(), [result("procedure_result:c", status=LabResultStatus.CORRECTED, observed_at=AFTER)]
    )
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.MATCHING_RESULT_FOUND
    assert "corrected" in match.summary


# --------------------------------------------------------------------------- #
# 8-9. Selection and conflict
# --------------------------------------------------------------------------- #


def test_latest_eligible_result_is_selected() -> None:
    """Guards: repeated results on different dates are not a conflict; the latest eligible one is cited."""
    context = with_results(
        load_context(),
        [
            result("procedure_result:late", value="7.4", observed_at=AFTER + timedelta(days=30)),
            result("procedure_result:early", value="8.9", observed_at=AFTER),
            result("procedure_result:prelim-later", status=LabResultStatus.PRELIMINARY, observed_at=AFTER + timedelta(days=60)),
        ],
    )
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.MATCHING_RESULT_FOUND
    lab_citations = [c for c in match.citations if c.record_type is RecordType.LAB_RESULT]
    assert [c.record_id for c in lab_citations] == ["procedure_result:late"]
    assert "7.4%" in match.summary


def test_conflicting_values_at_same_latest_timestamp_return_conflicting_records() -> None:
    """Guards: two final results at the identical latest time with different values are shown, not reconciled."""
    context = with_results(
        load_context(),
        [
            result("procedure_result:a", value="8.9", observed_at=AFTER),
            result("procedure_result:b", value="7.1", observed_at=AFTER),
            result("procedure_result:older", value="9.5", observed_at=AFTER - timedelta(days=10)),
        ],
    )
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.CONFLICTING_RECORDS
    assert {c.record_id for c in match.citations} == {"form_soap:1001", "procedure_result:a", "procedure_result:b"}


def test_identical_duplicates_at_same_timestamp_are_not_a_conflict() -> None:
    """Guards: duplicate rows (AUDIT DATA-004) with the same value collapse to one deterministic selection."""
    context = with_results(
        load_context(),
        [result("procedure_result:dup2", observed_at=AFTER), result("procedure_result:dup1", observed_at=AFTER)],
    )
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.MATCHING_RESULT_FOUND
    lab_citations = [c.record_id for c in match.citations if c.record_type is RecordType.LAB_RESULT]
    assert lab_citations == ["procedure_result:dup2"]  # highest record id at the shared timestamp


# --------------------------------------------------------------------------- #
# 10-11. Unsupported inputs
# --------------------------------------------------------------------------- #


def test_commitment_without_test_name_returns_ambiguous_match() -> None:
    """Boundary (ARCHITECTURE.md section 9): 'labs' with no named test is ambiguous_match - not absence,
    and not a source failure. Only the note is cited."""
    match = single(load_context(), extraction(hba1c_commitment(test_name=None)))
    assert match.state is EvidenceState.AMBIGUOUS_MATCH
    assert [c.record_type for c in match.citations] == [RecordType.PRIOR_NOTE]


def test_medication_commitment_without_records_is_no_matching_record_found() -> None:
    """Boundary: an empty (but available) medications source is a scoped absence claim citing only the note."""
    med = ExtractedCommitment(
        commitment_id="commitment-002",
        kind=CommitmentKind.MEDICATION,
        source_span="Continue metformin.",
        drug_name="metformin",
    )
    match = single(load_context(), extraction(med))
    assert match.state is EvidenceState.NO_MATCHING_RECORD_FOUND
    assert [c.record_type for c in match.citations] == [RecordType.PRIOR_NOTE]
    assert match.commitment.commitment_id == "commitment-002"


# --------------------------------------------------------------------------- #
# 12-13. Citations and purity
# --------------------------------------------------------------------------- #


def test_successful_match_cites_note_and_selected_result_with_source_timestamps() -> None:
    """Invariant: citations carry the source ids and timestamps from the input contracts, nothing invented."""
    context = load_context()
    match = single(context, extraction(hba1c_commitment()))
    by_type = {c.record_type: c for c in match.citations}
    assert by_type[RecordType.PRIOR_NOTE].record_id == context.prior_note.note_id
    assert by_type[RecordType.PRIOR_NOTE].timestamp == context.prior_note.note_date
    assert by_type[RecordType.LAB_RESULT].record_id == context.lab_results[0].result_id
    assert by_type[RecordType.LAB_RESULT].timestamp == context.lab_results[0].observed_at


def test_inputs_are_not_mutated_and_output_is_stable() -> None:
    """Invariant: pure function - inputs unchanged, identical inputs give identical outputs."""
    context = load_context()
    ext = extraction(hba1c_commitment())
    before_ctx, before_ext = copy.deepcopy(context.model_dump()), copy.deepcopy(ext.model_dump())
    first = match_evidence(context, ext)
    second = match_evidence(context, ext)
    assert context.model_dump() == before_ctx
    assert ext.model_dump() == before_ext
    assert [m.model_dump() for m in first] == [m.model_dump() for m in second]
    assert first[0].commitment is not ext.commitments[0]


def test_one_match_per_commitment_in_input_order() -> None:
    """Invariant: every commitment gets exactly one match, in the order supplied."""
    med = ExtractedCommitment(commitment_id="c-med", kind=CommitmentKind.MEDICATION, source_span="Continue metformin.", drug_name="metformin")
    matches = match_evidence(load_context(), extraction(med, hba1c_commitment()))
    assert [m.commitment.commitment_id for m in matches] == ["c-med", "commitment-001"]
    assert [m.state for m in matches] == [EvidenceState.NO_MATCHING_RECORD_FOUND, EvidenceState.MATCHING_RESULT_FOUND]


# --------------------------------------------------------------------------- #
# Source availability
# --------------------------------------------------------------------------- #


def with_unavailable(context: ContextBundle, *sources: EvidenceSource) -> ContextBundle:
    return context.model_copy(update={"data_quality": DataQuality(sources_unavailable=list(sources))}, deep=True)


def test_fixture_declares_all_sources_available() -> None:
    """Regression guard: the fixture is explicit that nothing was unavailable, and the default agrees."""
    context = load_context()
    assert context.data_quality.sources_unavailable == []
    assert DataQuality().sources_unavailable == []


def test_context_without_data_quality_is_still_valid() -> None:
    """Boundary: the field is backward compatible - older bundles without it validate and mean 'available'."""
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    del payload["context"]["data_quality"]
    context = BriefingRequest.model_validate(payload).context
    assert context.data_quality.sources_unavailable == []
    assert single(context, extraction(hba1c_commitment())).state is EvidenceState.MATCHING_RESULT_FOUND


def test_unavailable_lab_source_returns_verification_unavailable_not_absence() -> None:
    """Guards: 'could not check' must never collapse into 'nothing found'."""
    context = with_unavailable(with_results(load_context(), []), EvidenceSource.LAB_RESULTS)
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.VERIFICATION_UNAVAILABLE
    assert "could not be checked" in match.summary
    assert "not performed" not in match.summary and "no subsequent" not in match.summary.lower()
    assert [c.record_type for c in match.citations] == [RecordType.PRIOR_NOTE]


def test_unavailable_lab_source_overrides_partial_records() -> None:
    """Guards: an explicitly unavailable source is unreliable even if partial (matching) rows were supplied."""
    context = with_unavailable(load_context(), EvidenceSource.LAB_RESULTS)  # fixture still has the HbA1c row
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.VERIFICATION_UNAVAILABLE
    assert all(c.record_type is RecordType.PRIOR_NOTE for c in match.citations)


def test_empty_but_available_lab_source_returns_no_matching_record_found() -> None:
    """Boundary: the distinction the contract now expresses - checked and empty is a scoped absence claim."""
    context = with_results(load_context(), [])
    assert context.data_quality.sources_unavailable == []
    assert single(context, extraction(hba1c_commitment())).state is EvidenceState.NO_MATCHING_RECORD_FOUND


def test_invalid_source_name_is_rejected() -> None:
    """Boundary: sources_unavailable is a closed vocabulary."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DataQuality.model_validate({"sources_unavailable": ["vitals"]})


# --------------------------------------------------------------------------- #
# Correction precedence
# --------------------------------------------------------------------------- #


def test_later_corrected_result_supersedes_earlier_final() -> None:
    """Rule 1: a corrected result with a later timestamp is simply the latest eligible result."""
    context = with_results(
        load_context(),
        [
            result("procedure_result:9001", value="8.9", status=LabResultStatus.FINAL, observed_at=AFTER),
            result("procedure_result:9002", value="8.4", status=LabResultStatus.CORRECTED, observed_at=AFTER + timedelta(days=1)),
        ],
    )
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.MATCHING_RESULT_FOUND
    assert [c.record_id for c in match.citations if c.record_type is RecordType.LAB_RESULT] == ["procedure_result:9002"]
    assert "8.4%" in match.summary and "corrected" in match.summary


def test_corrected_version_preferred_over_final_with_same_result_id() -> None:
    """Rule 2: same result_id = same logical result; the corrected version wins even at the same timestamp."""
    context = with_results(
        load_context(),
        [
            result("procedure_result:9001", value="8.9", status=LabResultStatus.FINAL, observed_at=AFTER),
            result("procedure_result:9001", value="8.4", status=LabResultStatus.CORRECTED, observed_at=AFTER),
        ],
    )
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.MATCHING_RESULT_FOUND
    lab = [c for c in match.citations if c.record_type is RecordType.LAB_RESULT]
    assert [c.record_id for c in lab] == ["procedure_result:9001"]
    assert "8.4%" in match.summary and "supersedes" in match.summary


def test_conflicting_corrected_versions_with_same_result_id_are_conflicting() -> None:
    """Rule 3: two corrections of one record that disagree cannot be resolved by the contract."""
    context = with_results(
        load_context(),
        [
            result("procedure_result:9001", value="8.9", status=LabResultStatus.FINAL, observed_at=AFTER),
            result("procedure_result:9001", value="8.4", status=LabResultStatus.CORRECTED, observed_at=AFTER + timedelta(hours=1)),
            result("procedure_result:9001", value="7.9", status=LabResultStatus.CORRECTED, observed_at=AFTER + timedelta(hours=2)),
        ],
    )
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.CONFLICTING_RECORDS
    lab_ids = [c.record_id for c in match.citations if c.record_type is RecordType.LAB_RESULT]
    assert lab_ids == ["procedure_result:9001"] * 3  # every version cited, side by side


def test_different_ids_at_same_timestamp_remain_conflicting_even_if_one_is_corrected() -> None:
    """Rules 4-5: no contract field proves a corrected record supersedes a final record with a different id."""
    context = with_results(
        load_context(),
        [
            result("procedure_result:9001", value="8.9", status=LabResultStatus.FINAL, observed_at=AFTER),
            result("procedure_result:9002", value="8.4", status=LabResultStatus.CORRECTED, observed_at=AFTER),
        ],
    )
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.CONFLICTING_RECORDS
    assert {c.record_id for c in match.citations} == {"form_soap:1001", "procedure_result:9001", "procedure_result:9002"}


def test_older_same_id_conflict_does_not_block_a_later_unambiguous_result() -> None:
    """Guards: selection is by latest logical result; an older unresolved record is not the answer."""
    context = with_results(
        load_context(),
        [
            result("procedure_result:9001", value="8.4", status=LabResultStatus.CORRECTED, observed_at=AFTER),
            result("procedure_result:9001", value="7.9", status=LabResultStatus.CORRECTED, observed_at=AFTER),
            result("procedure_result:9003", value="7.2", status=LabResultStatus.FINAL, observed_at=AFTER + timedelta(days=30)),
        ],
    )
    match = single(context, extraction(hba1c_commitment()))
    assert match.state is EvidenceState.MATCHING_RESULT_FOUND
    assert [c.record_id for c in match.citations if c.record_type is RecordType.LAB_RESULT] == ["procedure_result:9003"]
