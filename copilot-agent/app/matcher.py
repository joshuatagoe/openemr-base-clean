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
* Evidence-name canonicalization is an internal detail of this module. The
  extractor returns the test name as supported by the note text; only the
  matcher normalizes names for comparison.

Initial scope: ``CommitmentKind.LAB_TEST`` against ``ContextBundle.lab_results``.
Medication commitments return ``verification_unavailable`` until medication
evidence is part of the contract.

Source availability: if ``EvidenceSource.LAB_RESULTS`` is listed in
``context.data_quality.sources_unavailable``, the source is treated as
unreliable even when partial records are present, and lab commitments return
``verification_unavailable``.

Correction precedence (what the current contract can support):

1. A corrected result with a later ``observed_at`` wins as the latest result.
2. Versions sharing a ``result_id`` are one logical result; a ``corrected``
   version is preferred over a ``final`` version of the same id.
3. Corrected versions of the same id that materially disagree are
   ``conflicting_records``.
4. Different ids at the same latest timestamp that disagree remain
   ``conflicting_records``: nothing in the contract proves one supersedes
   the other, and a corrected record is not assumed to supersede a final
   record with a different id.

A future ``report_id``, version number or ``supersedes_result_id`` field would
allow more precise correction handling (AUDIT DATA-005).

States not reachable with the current contract, by design:

* ``order_found_no_result`` - the bundle carries no structured lab orders yet.
  Producing it requires a contract extension (an ``orders`` collection); the
  matcher will not invent an order from a result or from note text.
* ``ambiguous_match`` - canonicalization is an exact alias map, so one
  commitment term resolves to at most one canonical test. This state becomes
  reachable when a term legitimately maps to several distinct tests (for
  example a panel name).
"""

from __future__ import annotations

import re
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
    LabResult,
    LabResultStatus,
    PriorNote,
    RecordType,
)

# --------------------------------------------------------------------------- #
# Canonicalization (matcher-internal)
# --------------------------------------------------------------------------- #

# Explicit alias map. Keys are *normalized* strings (see ``_normalize``);
# values are the internal canonical test key. No fuzzy matching: a name that
# is not in this map canonicalizes to its own normalized form and matches only
# an identical normalized form.
_TEST_ALIASES: dict[str, str] = {
    "hba1c": "hba1c",
    "hemoglobin a1c": "hba1c",
    "a1c": "hba1c",
    "glycated hemoglobin": "hba1c",
}

# Display label per canonical key, used only in summaries.
_CANONICAL_DISPLAY: dict[str, str] = {
    "hba1c": "HbA1c",
}

# Result statuses that count as completed evidence (AUDIT DATA-005).
_ELIGIBLE_STATUSES: frozenset[LabResultStatus] = frozenset(
    {LabResultStatus.FINAL, LabResultStatus.CORRECTED}
)

_PUNCTUATION = re.compile(r"[.,;:()\[\]{}\"'/\\_-]+")
_WHITESPACE = re.compile(r"\s+")


def _normalize(name: str) -> str:
    """Lowercase, strip harmless punctuation, collapse whitespace."""
    lowered = name.strip().lower()
    without_punct = _PUNCTUATION.sub(" ", lowered)
    return _WHITESPACE.sub(" ", without_punct).strip()


def _canonical_test_name(name: str | None) -> str | None:
    """Return the canonical test key for ``name``, or ``None`` if unusable."""
    if name is None:
        return None
    normalized = _normalize(name)
    if not normalized:
        return None
    return _TEST_ALIASES.get(normalized, normalized)


def _display_name(canonical: str, fallback: str) -> str:
    return _CANONICAL_DISPLAY.get(canonical, fallback)


# --------------------------------------------------------------------------- #
# Citations and summaries
# --------------------------------------------------------------------------- #


def _cite_note(note: PriorNote) -> Citation:
    return Citation(record_type=RecordType.PRIOR_NOTE, record_id=note.note_id, timestamp=note.note_date)


def _cite_result(result: LabResult) -> Citation:
    return Citation(record_type=RecordType.LAB_RESULT, record_id=result.result_id, timestamp=result.observed_at)


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
    conflicting = _materially_conflict(chosen)
    return _Resolved(
        result_id=result_id,
        latest_at=max(v.observed_at for v in ordered),
        representative=chosen[-1],
        versions=tuple(ordered),
        conflicting=conflicting,
        superseded_final=bool(corrected) and len(corrected) < len(ordered),
    )


# --------------------------------------------------------------------------- #
# Per-commitment evaluation
# --------------------------------------------------------------------------- #


def _unavailable(
    commitment: ExtractedCommitment, note: PriorNote, reason: str, extra: Iterable[LabResult] = ()
) -> EvidenceMatch:
    return EvidenceMatch(
        commitment=commitment.model_copy(deep=True),
        state=EvidenceState.VERIFICATION_UNAVAILABLE,
        summary=reason,
        citations=[_cite_note(note), *_cite_results(extra)],
    )


def _conflict(commitment: ExtractedCommitment, note: PriorNote, summary: str, cited: Iterable[LabResult]) -> EvidenceMatch:
    return EvidenceMatch(
        commitment=commitment.model_copy(deep=True),
        state=EvidenceState.CONFLICTING_RECORDS,
        summary=summary,
        citations=[_cite_note(note), *_cite_results(cited)],
    )


def _match_lab_test(commitment: ExtractedCommitment, context: ContextBundle) -> EvidenceMatch:
    note = context.prior_note

    if EvidenceSource.LAB_RESULTS in context.data_quality.sources_unavailable:
        return _unavailable(
            commitment,
            note,
            "The lab results source could not be checked, so verification of this commitment could not be completed.",
        )

    canonical = _canonical_test_name(commitment.test_name)
    if canonical is None:
        return _unavailable(
            commitment,
            note,
            "The commitment does not name a specific test, so no result could be checked.",
        )
    label = _display_name(canonical, commitment.test_name or "the requested test")

    # Strictly after the prior note; same canonical name only.
    candidates = [
        r
        for r in context.lab_results
        if r.observed_at > note.note_date and _canonical_test_name(r.test_name) == canonical
    ]
    if not candidates:
        return EvidenceMatch(
            commitment=commitment.model_copy(deep=True),
            state=EvidenceState.NO_MATCHING_RECORD_FOUND,
            summary=f"No subsequent matching {label} result was found in the supplied records.",
            citations=[_cite_note(note)],
        )

    eligible = [r for r in candidates if r.status in _ELIGIBLE_STATUSES]
    if not eligible:
        return _unavailable(
            commitment,
            note,
            f"Only non-final {label} results were found after the prior plan; a completed result could not be confirmed.",
            sorted(candidates, key=_version_key),
        )

    # Group versions by result id, resolve each logical result, then pick the latest.
    by_id: dict[str, list[LabResult]] = {}
    for r in eligible:
        by_id.setdefault(r.result_id, []).append(r)
    resolved = sorted((_resolve_versions(rid, vs) for rid, vs in by_id.items()), key=lambda x: (x.latest_at, x.result_id))

    latest_at = resolved[-1].latest_at
    latest = [x for x in resolved if x.latest_at == latest_at]

    # Rule 3: conflicting versions of the same logical result.
    same_id_conflicts = [x for x in latest if x.conflicting]
    if same_id_conflicts:
        cited = [v for x in same_id_conflicts for v in x.versions]
        return _conflict(
            commitment,
            note,
            f"Versions of the same {label} record disagree on the recorded value; they are shown side by side and not reconciled.",
            cited,
        )

    # Rule 4: different ids at the same latest timestamp that disagree.
    representatives = [x.representative for x in latest]
    if len(latest) > 1 and _materially_conflict(representatives):
        return _conflict(
            commitment,
            note,
            f"{len(latest)} {label} results share the latest timestamp but record different values; "
            "they are shown side by side and not reconciled.",
            representatives,
        )

    # Identical duplicates across ids collapse to one deterministic pick (highest id).
    chosen = latest[-1]
    selected = chosen.representative
    summary = f"A {selected.status.value} {label} result of {_format_value(selected)} was recorded after the prior plan."
    if selected.abnormal_flag is not None:
        summary += f" Abnormal flag as recorded: {selected.abnormal_flag.value}."
    if chosen.superseded_final:
        summary += " This corrected version supersedes an earlier final version of the same record."
    if len(latest) > 1:
        summary += f" {len(latest)} identical records share this timestamp."
    return EvidenceMatch(
        commitment=commitment.model_copy(deep=True),
        state=EvidenceState.MATCHING_RESULT_FOUND,
        summary=summary,
        citations=[_cite_note(note), _cite_result(selected)],
    )


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
        else:
            matches.append(
                _unavailable(
                    commitment,
                    context.prior_note,
                    f"Evidence matching for '{commitment.kind.value}' commitments is not supported yet; "
                    "this commitment was not evaluated.",
                )
            )
    return matches


__all__ = ["match_evidence"]
