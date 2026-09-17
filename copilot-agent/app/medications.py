"""Deterministic medication matcher (ARCHITECTURE.md sections 8, 9 and 11; AUDIT DATA-001/003).

Rules, in order, for a ``medication`` commitment:

1. Medications source unavailable -> ``verification_unavailable``.
2. No drug name -> ``ambiguous_match``.
3. Candidates = records from BOTH tables whose RxNorm code equals the
   commitment's (when the commitment carries one - it does not yet) or whose
   normalized drug name contains the commitment's normalized ingredient name
   as a whole word. None -> ``no_matching_record_found``.
4. The two sources disagree on status for the drug (one active, one inactive)
   -> ``conflicting_records``, both cited side by side.
5. Direction is verified from record fields only:
   - start:    a candidate started strictly after the note -> found; candidates
               exist but all started before the note -> ``ambiguous_match``.
   - stop:     a candidate ended after the note, or inactive with a modification
               after the note -> found; only active candidates with no end ->
               ``conflicting_records`` (record contradicts the note);
               otherwise ``ambiguous_match``.
   - continue: an active candidate -> found; only inactive candidates ->
               ``conflicting_records``; only indeterminate -> ``ambiguous_match``.
   - increase / decrease / switch: direction cannot be verified from stored
               fields (dosage is free text, no pre-baseline snapshot) ->
               ``ambiguous_match`` with candidates shown.
   - unclear:  ``ambiguous_match`` with candidates shown.

Nothing here parses dosage text or interprets drug names beyond whole-word
containment; the module keeps both tables' rows so that disagreement is
visible rather than resolved.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.contracts import (
    Citation,
    ContextBundle,
    EvidenceMatch,
    EvidenceSource,
    EvidenceState,
    ExtractedCommitment,
    MedicationAction,
    MedicationRecord,
    RecordType,
)
from app.synonyms import normalize

_WORD = re.compile(r"[a-z0-9]+")


def _cite_note(context: ContextBundle) -> Citation:
    note = context.prior_note
    return Citation(record_type=RecordType.PRIOR_NOTE, record_id=note.note_id, timestamp=note.note_date)


def _cite(record: MedicationRecord) -> Citation:
    return Citation(record_type=RecordType.MEDICATION, record_id=record.record_id, timestamp=record.timestamp)


def _cites(records: Iterable[MedicationRecord]) -> list[Citation]:
    return [_cite(r) for r in sorted(records, key=lambda r: (r.timestamp, r.record_id))]


def ingredient_key(name: str) -> str:
    """First alphanumeric word of the normalized name: 'Metformin 500 mg' -> 'metformin'."""
    words = _WORD.findall(normalize(name))
    return words[0] if words else ""


def _name_matches(record: MedicationRecord, key: str) -> bool:
    return key != "" and key in _WORD.findall(normalize(record.drug_name))


def _match(commitment: ExtractedCommitment, context: ContextBundle, state: EvidenceState, summary: str, cited: Iterable[MedicationRecord] = (), candidates: Iterable[MedicationRecord] = ()) -> EvidenceMatch:
    return EvidenceMatch(
        commitment=commitment.model_copy(deep=True),
        state=state,
        summary=summary,
        citations=[_cite_note(context), *_cites(cited)],
        candidates=_cites(candidates),
    )


def _label(record: MedicationRecord) -> str:
    status = "active" if record.active else ("inactive" if record.active is False else "indeterminate status")
    return f"{record.drug_name} ({record.source_table.value}, {status})"


def match_medication(commitment: ExtractedCommitment, context: ContextBundle) -> EvidenceMatch:
    note_date = context.prior_note.note_date
    if EvidenceSource.MEDICATIONS in context.data_quality.sources_unavailable:
        return _match(commitment, context, EvidenceState.VERIFICATION_UNAVAILABLE, "The medications source could not be checked, so verification of this commitment could not be completed.")

    key = ingredient_key(commitment.drug_name or "")
    if not key:
        return _match(commitment, context, EvidenceState.AMBIGUOUS_MATCH, "The plan states a medication action without naming the drug, so no record could be searched for.")

    candidates = [r for r in context.medications if _name_matches(r, key)]
    if not candidates:
        return _match(commitment, context, EvidenceState.NO_MATCHING_RECORD_FOUND, f"No medication record for '{commitment.drug_name}' was found in either source (no evidence in this system).")

    # Rule 4: the two tables disagree on status for this drug.
    active_sources = {r.source_table for r in candidates if r.active is True}
    inactive_sources = {r.source_table for r in candidates if r.active is False}
    if active_sources and inactive_sources and active_sources != inactive_sources:
        return _match(
            commitment,
            context,
            EvidenceState.CONFLICTING_RECORDS,
            f"The prescriptions and medication-list records for '{commitment.drug_name}' disagree on status; they are shown side by side and not reconciled.",
            cited=candidates,
        )

    action = commitment.action or MedicationAction.UNCLEAR
    active = [r for r in candidates if r.active is True]
    inactive = [r for r in candidates if r.active is False]

    if action is MedicationAction.START:
        started_after = [r for r in candidates if r.started_at is not None and r.started_at > note_date]
        if started_after:
            latest = max(started_after, key=lambda r: (r.started_at or r.timestamp, r.record_id))
            return _match(commitment, context, EvidenceState.MATCHING_MEDICATION_RECORD_FOUND, f"A record for {_label(latest)} was started on {latest.started_at:%Y-%m-%d}, after the prior plan.", cited=[latest], candidates=[r for r in started_after if r is not latest])
        return _match(commitment, context, EvidenceState.AMBIGUOUS_MATCH, f"Records for '{commitment.drug_name}' exist but none started after the prior plan, so the start could not be verified from record dates.", candidates=candidates)

    if action is MedicationAction.STOP:
        ended_after = [r for r in candidates if (r.ended_at is not None and r.ended_at > note_date) or (r.active is False and r.modified_at is not None and r.modified_at > note_date)]
        if ended_after:
            latest = max(ended_after, key=lambda r: (r.ended_at or r.modified_at or r.timestamp, r.record_id))
            return _match(commitment, context, EvidenceState.MATCHING_MEDICATION_RECORD_FOUND, f"A record for {_label(latest)} was ended or deactivated after the prior plan.", cited=[latest], candidates=[r for r in ended_after if r is not latest])
        if active and not inactive:
            return _match(commitment, context, EvidenceState.CONFLICTING_RECORDS, f"The plan states stopping '{commitment.drug_name}' but the record still shows it active with no end date; shown side by side and not reconciled.", cited=active)
        return _match(commitment, context, EvidenceState.AMBIGUOUS_MATCH, f"Records for '{commitment.drug_name}' exist but no end or deactivation after the prior plan could be verified from record fields.", candidates=candidates)

    if action is MedicationAction.CONTINUE:
        if active:
            latest = max(active, key=lambda r: (r.timestamp, r.record_id))
            return _match(commitment, context, EvidenceState.MATCHING_MEDICATION_RECORD_FOUND, f"An active record for {_label(latest)} is on file.", cited=[latest], candidates=[r for r in active if r is not latest])
        if inactive:
            return _match(commitment, context, EvidenceState.CONFLICTING_RECORDS, f"The plan states continuing '{commitment.drug_name}' but the only records on file are inactive; shown side by side and not reconciled.", cited=inactive)
        return _match(commitment, context, EvidenceState.AMBIGUOUS_MATCH, f"Records for '{commitment.drug_name}' exist but their status fields disagree, so continuation could not be verified.", candidates=candidates)

    if action in (MedicationAction.INCREASE, MedicationAction.DECREASE, MedicationAction.SWITCH):
        return _match(commitment, context, EvidenceState.AMBIGUOUS_MATCH, f"A {action.value} of '{commitment.drug_name}' cannot be verified from stored fields (dose is free text); matching records are shown.", candidates=candidates)

    return _match(commitment, context, EvidenceState.AMBIGUOUS_MATCH, f"The plan's wording for '{commitment.drug_name}' does not state an action that can be checked; matching records are shown.", candidates=candidates)


__all__ = ["ingredient_key", "match_medication"]
