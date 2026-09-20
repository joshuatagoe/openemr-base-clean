"""Deterministic evidence matcher.

Compares already-extracted commitments (``ExtractionOutput``) with already-
structured evidence (``ContextBundle``) and assigns one source-specific
``EvidenceState`` per commitment (ARCHITECTURE.md sections 8 and 9).

Properties:

* Pure and synchronous: no I/O, no network, no model call, no logging.
* Inputs are never mutated; outputs are validated Pydantic models.
* Stable: identical inputs yield identical outputs (all selection is by
  timestamp then record id, never by list position or dict order).
* Never reads ``plan_text``. Commitments arrive structured; the matcher does
  not parse clinical language.
* ``no_matching_record_found`` means only that the supplied, available
  records contain no match. It is never a claim that the commitment was not
  carried out.
* Name resolution goes through the curated synonym table (``app.synonyms``).
  A commitment whose test cannot be resolved is ``ambiguous_match`` - absence
  cannot be asserted for a test that could not be searched for.

Lab/test rules, in order (section 9):

1. Lab results source unavailable -> ``verification_unavailable``.
2. No test name, or a name the table cannot resolve -> ``ambiguous_match``.
3. Eligible (final/corrected) results after the note whose canonical key is
   one the commitment resolves to -> ``matching_result_found`` (latest per
   key; conflicting versions -> ``conflicting_records``). Only preliminary
   or incomplete results -> ``verification_unavailable`` with candidates.
4. No result, but a non-canceled order after the note for the same key ->
   ``order_found_no_result`` (if the orders source is unavailable at this
   point -> ``verification_unavailable``).
5. Otherwise ``no_matching_record_found``.

Medication commitments are matched by ``app.medications`` (both source
tables, direction from record fields). ``other`` commitments are never
checked: they carry no state.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.contracts import (
    Citation,
    CommitmentKind,
    ContextBundle,
    EvidenceMatch,
    EvidenceSource,
    EvidenceState,
    ExtractedCommitment,
    ExtractionOutput,
    LabOrder,
    LabOrderStatus,
    LabResult,
    LabResultStatus,
    PriorNote,
    RecordType,
)
from app.medications import match_medication
from app.synonyms import Resolution, display_name, normalize, resolve_commitment, resolve_record, resolve_record_panel

# Result statuses that count as completed evidence (AUDIT DATA-005).
_ELIGIBLE_STATUSES: frozenset[LabResultStatus] = frozenset({LabResultStatus.FINAL, LabResultStatus.CORRECTED})


# --------------------------------------------------------------------------- #
# Citations and summaries
# --------------------------------------------------------------------------- #


def _cite_note(note: PriorNote) -> Citation:
    return Citation(record_type=RecordType.PRIOR_NOTE, record_id=note.note_id, timestamp=note.note_date)


def _cite_result(result: LabResult) -> Citation:
    return Citation(record_type=RecordType.LAB_RESULT, record_id=result.result_id, timestamp=result.observed_at)


def _cite_order(order: LabOrder) -> Citation:
    return Citation(record_type=RecordType.LAB_ORDER, record_id=order.record_id, timestamp=order.ordered_at)


def _cite_results(results: Iterable[LabResult]) -> list[Citation]:
    return [_cite_result(r) for r in results]


def _format_value(result: LabResult) -> str:
    value = f"{result.value}"
    if result.units is None:
        return f"{value} (units not recorded)"
    if result.units == "%":
        return f"{value}%"
    return f"{value} {result.units}"


def _version_key(result: LabResult) -> tuple:
    """Deterministic order for versions sharing a result id."""
    return (result.observed_at, result.status.value, result.units or "", str(result.value))


def _materially_conflict(results: Sequence[LabResult]) -> bool:
    """True when results disagree on value or units."""
    signatures = {(Decimal(r.value), r.units) for r in results}
    return len(signatures) > 1


def _record_key(test_name: str, code: str | None) -> str:
    """Canonical key for an evidence record: curated table first, else its own normalized name."""
    return resolve_record(test_name, code) or normalize(test_name)


def _order_matches(order: LabOrder, keys: frozenset[str]) -> bool:
    """An order matches by its own key, or when it is named as a panel containing a wanted key."""
    return _record_key(order.test_name, order.code) in keys or bool(resolve_record_panel(order.test_name) & keys)


# --------------------------------------------------------------------------- #
# Version resolution per logical result (same result_id)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Resolved:
    """One logical result after version resolution."""

    result_id: str
    latest_at: datetime
    representative: LabResult
    versions: tuple[LabResult, ...]
    conflicting: bool
    superseded_final: bool


def _resolve_versions(result_id: str, versions: Sequence[LabResult]) -> _Resolved:
    ordered = sorted(versions, key=_version_key)
    corrected = [v for v in ordered if v.status is LabResultStatus.CORRECTED]
    chosen = corrected if corrected else ordered
    return _Resolved(
        result_id=result_id,
        latest_at=max(v.observed_at for v in ordered),
        representative=chosen[-1],
        versions=tuple(ordered),
        conflicting=_materially_conflict(chosen),
        superseded_final=bool(corrected) and len(corrected) < len(ordered),
    )


@dataclass(frozen=True)
class _KeyOutcome:
    """Outcome of matching one canonical key: either a selected result or a conflict."""

    key: str
    selected: LabResult | None
    conflict_cited: tuple[LabResult, ...]
    conflict_reason: str | None
    duplicates: int
    superseded_final: bool


def _match_key(key: str, eligible: Sequence[LabResult]) -> _KeyOutcome:
    """Pick the latest eligible result for one key, or report a conflict."""
    by_id: dict[str, list[LabResult]] = {}
    for r in eligible:
        by_id.setdefault(r.result_id, []).append(r)
    resolved = sorted((_resolve_versions(rid, vs) for rid, vs in by_id.items()), key=lambda x: (x.latest_at, x.result_id))
    latest_at = resolved[-1].latest_at
    latest = [x for x in resolved if x.latest_at == latest_at]

    same_id_conflicts = [x for x in latest if x.conflicting]
    if same_id_conflicts:
        cited = tuple(v for x in same_id_conflicts for v in x.versions)
        return _KeyOutcome(key, None, cited, "versions of the same record disagree on the recorded value", 0, False)

    representatives = [x.representative for x in latest]
    if len(latest) > 1 and _materially_conflict(representatives):
        return _KeyOutcome(key, None, tuple(representatives), f"{len(latest)} results share the latest timestamp but record different values", 0, False)

    chosen = latest[-1]
    return _KeyOutcome(key, chosen.representative, (), None, len(latest) - 1, chosen.superseded_final)


# --------------------------------------------------------------------------- #
# Per-commitment evaluation
# --------------------------------------------------------------------------- #


def _match(
    commitment: ExtractedCommitment,
    state: EvidenceState | None,
    summary: str,
    citations: Iterable[Citation] = (),
    candidates: Iterable[Citation] = (),
) -> EvidenceMatch:
    return EvidenceMatch(
        commitment=commitment.model_copy(deep=True),
        state=state,
        summary=summary,
        citations=list(citations),
        candidates=list(candidates),
    )


def _describe_result(selected: LabResult, label: str, outcome: _KeyOutcome) -> str:
    text = f"A {selected.status.value} {label} result of {_format_value(selected)} was recorded after the prior plan."
    if selected.abnormal_flag is not None:
        text += f" Abnormal flag as recorded: {selected.abnormal_flag.value}."
    if outcome.superseded_final:
        text += " This corrected version supersedes an earlier final version of the same record."
    if outcome.duplicates:
        text += f" {outcome.duplicates + 1} identical records share this timestamp."
    return text


def _match_lab_test(commitment: ExtractedCommitment, context: ContextBundle) -> EvidenceMatch:
    note = context.prior_note
    unavailable = set(context.data_quality.sources_unavailable)
    note_cite = _cite_note(note)

    if EvidenceSource.LAB_RESULTS in unavailable:
        return _match(
            commitment,
            EvidenceState.VERIFICATION_UNAVAILABLE,
            "The lab results source could not be checked, so verification of this commitment could not be completed.",
            [note_cite],
        )

    resolution: Resolution | None = resolve_commitment(commitment.test_name)
    if commitment.test_name is None or not normalize(commitment.test_name):
        return _match(
            commitment,
            EvidenceState.AMBIGUOUS_MATCH,
            "The plan commits to testing without naming a specific test, so no record could be searched for.",
            [note_cite],
        )
    if resolution is None:
        # Not in the curated table. An exact normalized-name match is still safe to use; otherwise the
        # test cannot be searched for and absence must not be asserted.
        exact = normalize(commitment.test_name)
        resolution = Resolution(display=commitment.test_name, keys=frozenset({exact}))
        known = any(_record_key(r.test_name, r.code) == exact for r in context.lab_results) or any(
            _order_matches(o, resolution.keys) for o in context.lab_orders
        )
        if not known:
            return _match(
                commitment,
                EvidenceState.AMBIGUOUS_MATCH,
                f"'{commitment.test_name}' is not in the curated test table and no record uses that exact name, so it could not be searched for.",
                [note_cite],
            )

    label = resolution.display
    keys = resolution.keys

    # Strictly after the prior note; same canonical key only.
    candidates = [r for r in context.lab_results if r.observed_at > note.note_date and _record_key(r.test_name, r.code) in keys]
    if candidates:
        eligible = [r for r in candidates if r.status in _ELIGIBLE_STATUSES]
        if not eligible:
            return _match(
                commitment,
                EvidenceState.VERIFICATION_UNAVAILABLE,
                f"Only non-final {label} results were found after the prior plan; a completed result could not be confirmed.",
                [note_cite, *_cite_results(sorted(candidates, key=_version_key))],
            )
        by_key: dict[str, list[LabResult]] = {}
        for r in eligible:
            by_key.setdefault(_record_key(r.test_name, r.code), []).append(r)
        outcomes = [_match_key(k, rs) for k, rs in sorted(by_key.items())]

        conflicts = [o for o in outcomes if o.conflict_reason is not None]
        if conflicts:
            first = conflicts[0]
            cited = [r for o in conflicts for r in o.conflict_cited]
            return _match(
                commitment,
                EvidenceState.CONFLICTING_RECORDS,
                f"{display_name(first.key)}: {first.conflict_reason}; they are shown side by side and not reconciled.",
                [note_cite, *_cite_results(cited)],
            )

        selected = [o for o in outcomes if o.selected is not None]
        if resolution.is_panel:
            names = ", ".join(display_name(o.key) for o in selected)
            summary = (
                f"{len(selected)} of {len(keys)} {label} components resulted after the prior plan: {names}. "
                "Values are shown from the cited records."
            )
            cites = [_cite_result(o.selected) for o in selected if o.selected is not None]
            # The panel's order is evidence too (and otherwise shows as unexplained in the interval list).
            seen_orders: set[str] = set()
            for o in selected:
                panel_order = _order_for(o.selected, context.lab_orders) if o.selected is not None else None
                if panel_order is not None and panel_order.record_id not in seen_orders:
                    seen_orders.add(panel_order.record_id)
                    cites.append(_cite_order(panel_order))
            return _match(commitment, EvidenceState.MATCHING_RESULT_FOUND, summary, [note_cite, *cites])

        outcome = selected[0]
        assert outcome.selected is not None
        cites = [note_cite, _cite_result(outcome.selected)]
        order = _order_for(outcome.selected, context.lab_orders)
        if order is not None:
            cites.append(_cite_order(order))
        return _match(commitment, EvidenceState.MATCHING_RESULT_FOUND, _describe_result(outcome.selected, label, outcome), cites)

    # No result: look for an order.
    if EvidenceSource.LAB_ORDERS in unavailable:
        return _match(
            commitment,
            EvidenceState.VERIFICATION_UNAVAILABLE,
            f"No {label} result was found after the prior plan and the orders source could not be checked.",
            [note_cite],
        )
    orders = sorted(
        (o for o in context.lab_orders if o.ordered_at > note.note_date and o.status is not LabOrderStatus.CANCELED and _order_matches(o, keys)),
        key=lambda o: (o.ordered_at, o.order_id, o.sequence),
    )
    if orders:
        latest = orders[-1]
        summary = f"A {label} order ({latest.status.value}) was placed after the prior plan; no result is on file for it."
        if len(orders) > 1:
            summary += f" {len(orders)} matching orders were found; the latest is cited."
        return _match(commitment, EvidenceState.ORDER_FOUND_NO_RESULT, summary, [note_cite, _cite_order(latest)], [_cite_order(o) for o in orders[:-1]])

    return _match(
        commitment,
        EvidenceState.NO_MATCHING_RECORD_FOUND,
        f"No {label} order or result was found in the supplied records after the prior plan (no evidence in this system).",
        [note_cite],
    )


def _order_for(result: LabResult, orders: Sequence[LabOrder]) -> LabOrder | None:
    if result.order_id is None:
        return None
    key = _record_key(result.test_name, result.code)
    same = [o for o in orders if o.order_id == result.order_id]
    for o in same:
        if _record_key(o.test_name, o.code) == key:
            return o
    # A panel order with several reports (e.g. final then corrected) arrives once per report; identical
    # rows are one order, so the result's order is still cited rather than left unexplained.
    distinct = {o.record_id for o in same}
    return same[0] if len(distinct) == 1 else None


# --------------------------------------------------------------------------- #
# Public interface
# --------------------------------------------------------------------------- #


def match_evidence(context: ContextBundle, extraction: ExtractionOutput) -> list[EvidenceMatch]:
    """Assign an evidence state to every extracted commitment.

    Returns exactly one ``EvidenceMatch`` per commitment, in input order.
    Neither argument is modified.
    """
    matches: list[EvidenceMatch] = []
    for commitment in extraction.commitments:
        if commitment.kind is CommitmentKind.LAB_TEST:
            matches.append(_match_lab_test(commitment, context))
        elif commitment.kind is CommitmentKind.MEDICATION:
            matches.append(match_medication(commitment, context))
        else:
            matches.append(_match(commitment, None, "Other plan text; not checked against the record.", [_cite_note(context.prior_note)]))
    return matches


__all__ = ["match_evidence"]
