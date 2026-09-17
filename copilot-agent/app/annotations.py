"""Interval evidence annotation (ARCHITECTURE.md sections 2 and 8).

The interval layer is every record dated after the baseline note: results,
orders and medication changes. It is listed deterministically by the module
and rendered before the agent is contacted. The agent's only contribution is
an annotation per record: which verified commitment, if any, accounts for it
(``explained_by``). A record no match cites or lists as a candidate is
``explained_by: null`` - shown as unexplained, never dropped.

Pure and deterministic: the first commitment (in output order) that cites or
lists the record wins.
"""

from __future__ import annotations

from app.contracts import ContextBundle, EvidenceMatch, IntervalAnnotation, RecordType


def interval_records(context: ContextBundle) -> list[tuple[str, RecordType]]:
    """Record ids dated strictly after the baseline note, in a stable order."""
    note_date = context.prior_note.note_date
    records: list[tuple[str, RecordType]] = []
    for r in sorted(context.lab_results, key=lambda x: (x.observed_at, x.result_id)):
        if r.observed_at > note_date:
            records.append((r.result_id, RecordType.LAB_RESULT))
    for o in sorted(context.lab_orders, key=lambda x: (x.ordered_at, x.order_id, x.sequence)):
        if o.ordered_at > note_date:
            records.append((o.record_id, RecordType.LAB_ORDER))
    for m in sorted(context.medications, key=lambda x: (x.timestamp, x.record_id)):
        if m.timestamp > note_date:
            records.append((m.record_id, RecordType.MEDICATION))
    # Versions sharing a result id are one logical record.
    seen: set[str] = set()
    unique: list[tuple[str, RecordType]] = []
    for rid, rtype in records:
        if rid not in seen:
            seen.add(rid)
            unique.append((rid, rtype))
    return unique


def annotate_interval(context: ContextBundle, matches: list[EvidenceMatch]) -> list[IntervalAnnotation]:
    """One annotation per interval record; ``explained_by`` is the first match that cites or lists it."""
    explained: dict[str, str] = {}
    for m in matches:
        for c in [*m.citations, *m.candidates]:
            if c.record_type is not RecordType.PRIOR_NOTE:
                explained.setdefault(c.record_id, m.commitment.commitment_id)
    return [
        IntervalAnnotation(record_id=rid, record_type=rtype, explained_by=explained.get(rid))
        for rid, rtype in interval_records(context)
    ]


__all__ = ["annotate_interval", "interval_records"]
