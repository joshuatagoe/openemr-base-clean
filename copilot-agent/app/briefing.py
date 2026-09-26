"""The grounded briefing: three headings, tiered assertions, screened considerations.

Feature PRD F11, with the tier rule from ADR-006 §4 / F07 §3.7.

    LabDocument (F03)  ─┐
    prior chart facts  ─┼─>  build_briefing  ─>  Briefing  ─>  render_briefing
    EvidencePackage    ─┘                          │
    candidate considerations                       └─ dropped claims, counted

**This is not a recommendation system and must never become one** (`USER.md` §6).
"Guidance states X, and this patient's A1c is above the range it names" is in
scope. "Increase the dose" is refused, in the fixed wording Week 1 already
settled, and any statement written in the directive voice is dropped before
display rather than trusted to the prompt.

Four rules shape everything below.

**Every displayed claim carries its tier.** :class:`AssertionTier` is the closed
set from F11 §3.2, and the tier decides which citation the claim must carry: a
document-stated claim cites the document, a chart fact cites the record, a
guideline-supported claim cites the passage. The model validators enforce the
pairing, so a claim cannot be constructed without the citation its tier implies.

**A computed comparison is never styled as a lab-printed flag.** This is the
single most consequential display error in the system (W2-AMB-055). A value
above its printed range with no printed flag becomes a :attr:`AssertionTier.COMPUTED`
line carrying a :class:`ComputedComparison` - its inputs and the rule that was
applied - and the renderer labels the two cases with two different, non-
interchangeable strings (:data:`COMPUTED_LABEL`, :data:`PRINTED_FLAG_LABEL`).

**Tier admissibility is enforced, not displayed.** A Tier B passage - patient
education grade - may support a care-process statement and may never support a
clinical threshold, target or decision boundary. The decision is
:func:`app.corpus.supports`, reused here rather than restated, so the briefing
and the corpus can never disagree about what Tier B may carry. A threshold claim
whose only support is Tier B is dropped **before display** and the drop is
counted.

**A gap in one topic never suppresses the others** (F11 §3.4). An uncovered
topic is named as a corpus limitation and every supported consideration is still
shown. The system never fills a gap from model knowledge.

This module consumes :class:`app.evidence.EvidencePackage` and nothing else from
the retrieval side (CR3): "what evidence did this briefing see?" has exactly one
answer, and it is the package that was logged.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from app.contracts import Citation as RecordCitation, MedicationRecord, RecordType, StrictModel
from app.corpus import ClaimKind, DropReason as CorpusDropReason, supports
from app.documents import (
    AbnormalFlag,
    AbnormalFlagSource,
    DocumentCitation,
    LabDocument,
    LabResult,
    VerificationStatus,
)
from app.evidence import EvidencePackage, EvidenceSnippet, GuidelineCitation, RetrievalStatus
from app.intake import LABEL_MEDICATION, IntakeForm, ReportedMedication, intake_items, medication_text
from app.lab_extractor import derive_abnormal_flag, parse_reference_range
from app.medications import records_named
from app.observability import log_event

# Week 1's verification spine, reused rather than restated. These three are
# private to `verifier` only in the sense that nothing outside `app` should
# import them: a second copy of the recommendation deny-list or of the numeric
# normalisation rule would drift from the one the Week 1 tests gate, and the
# drift would be invisible until a directive statement reached a physician.
from app.verifier import (
    ADVICE_TOPIC_PATTERN as _ADVICE_TOPIC,
    NUMBER_PATTERN as _NUMBER,
    RECOMMENDATION_PATTERN as _RECOMMENDATION,
    canonical_refusal,
    normalize_number as _normalize_number,
)

# --------------------------------------------------------------------------- #
# Fixed display strings
# --------------------------------------------------------------------------- #

#: The three headings, always rendered, in this order (F11 §3.1).
HEADINGS: tuple[str, str, str] = ("What changed", "Needs attention", "What to consider")

#: Said plainly when a heading, or the whole briefing, has nothing in it.
#: A thin briefing is a finding, not a failure (F11 §8).
NOTHING_TO_REPORT = "Nothing to report from the supplied records."

#: The two labels that must never be interchangeable. One means the lab printed
#: it; the other means this system worked it out.
COMPUTED_LABEL = "computed by this system"
PRINTED_FLAG_LABEL = "flag printed on the report"

#: The rule a computed comparison shows alongside its inputs.
COMPARISON_RULE = "the value was compared with the reference range printed on the source document"

EVIDENCE_UNAVAILABLE_LIMITATION = (
    "No guideline evidence was retrieved, so no guideline-supported consideration is shown. "
    "This is a limitation of the retrieval, not a finding about this patient."
)

RETRIEVAL_DEGRADED_LIMITATION = (
    "Guideline retrieval was degraded for this briefing, so the considerations shown may be incomplete."
)

#: Marks a value read from a document that is not yet part of the record (F11 §3.6).
NOT_YET_IN_CHART = "not yet in the chart"


def uncovered_topic_limitation(topic: str) -> str:
    """Name a topic the selected corpus does not address (F11 §3.4).

    The silence has to be attributable: a physician must be able to tell a
    corpus gap from an absence of concern.
    """
    return f'The selected guideline corpus does not address "{topic}", so no consideration is shown for it.'


# --------------------------------------------------------------------------- #
# Tiers
# --------------------------------------------------------------------------- #


class AssertionTier(StrEnum):
    """What kind of thing an assertion is, and therefore how it must be cited (F11 §3.2)."""

    #: Printed in the source document.
    DOCUMENT_STATED = "document_stated"
    #: Already in the record.
    CHART_FACT = "chart_fact"
    #: Derived here by a deterministic rule. Never styled as a lab-printed flag.
    COMPUTED = "computed"
    #: Stated by the patient on an intake form. An observation, never a finding.
    PATIENT_REPORTED = "patient_reported"
    #: What the guideline says, quoted and attributed.
    GUIDELINE_SUPPORTED = "guideline_supported"

    @property
    def label(self) -> str:
        return _TIER_LABELS[self]


_TIER_LABELS = {
    AssertionTier.DOCUMENT_STATED: "document-stated",
    AssertionTier.CHART_FACT: "chart fact",
    AssertionTier.COMPUTED: "computed",
    AssertionTier.PATIENT_REPORTED: "patient-reported",
    AssertionTier.GUIDELINE_SUPPORTED: "guideline-supported",
}


class DropReason(StrEnum):
    """Why an assertion was withheld before display. Every one is counted.

    The first two carry the corpus's own values deliberately: a drop counted
    here and a drop counted by :meth:`app.corpus.Corpus.screen_claims` are the
    same event seen at two layers, and should aggregate.
    """

    TIER_INADMISSIBLE = CorpusDropReason.TIER_INADMISSIBLE.value
    UNRESOLVABLE_CITATION = CorpusDropReason.UNRESOLVABLE_CITATION.value
    NUMERIC_UNSUPPORTED = "numeric_unsupported"
    DIRECTIVE_LANGUAGE = "directive_language"


# --------------------------------------------------------------------------- #
# Displayed shapes
# --------------------------------------------------------------------------- #


class ComputedComparison(StrictModel):
    """The inputs and the rule behind a comparison this system made.

    Shown whenever a computed line is expanded, so the physician can check the
    arithmetic rather than take the label on trust (F11 §5 AC-2).
    """

    test_name: str = Field(min_length=1)
    value: str = Field(min_length=1, description="The value exactly as printed on the document.")
    unit: str | None = None
    reference_range: str = Field(min_length=1, description="The range exactly as printed.")
    direction: Literal["above", "below"]
    rule: str = COMPARISON_RULE


class CitedFact(StrictModel):
    """One patient fact a consideration rests on, with its tier and its citation."""

    text: str = Field(min_length=1)
    tier: AssertionTier
    record_citation: RecordCitation | None = None
    document_citation: DocumentCitation | None = None
    not_yet_in_chart: bool = False

    @model_validator(mode="after")
    def _citation_matches_tier(self) -> CitedFact:
        if self.tier is AssertionTier.GUIDELINE_SUPPORTED:
            raise ValueError("a patient fact is never guideline-supported")
        if self.tier is AssertionTier.CHART_FACT and self.record_citation is None:
            raise ValueError("a chart fact must cite the record it came from")
        if self.tier in (AssertionTier.DOCUMENT_STATED, AssertionTier.PATIENT_REPORTED) and self.document_citation is None:
            raise ValueError(f"a {self.tier.value} fact must cite the document it was read from")
        if self.tier is AssertionTier.COMPUTED and self.record_citation is None and self.document_citation is None:
            raise ValueError("a computed fact must cite the inputs it was derived from")
        return self


class BriefingLine(StrictModel):
    """One displayed assertion under *What changed* or *Needs attention*.

    The tier decides which citation is mandatory; the validator makes an
    uncited or mis-cited line unconstructable rather than merely unrendered.
    """

    line_id: str = Field(min_length=1)
    tier: AssertionTier
    text: str = Field(min_length=1)
    document_citation: DocumentCitation | None = None
    record_citation: RecordCitation | None = None
    computed: ComputedComparison | None = None
    abnormal_flag: AbnormalFlag | None = None
    abnormal_flag_source: AbnormalFlagSource | None = None
    not_yet_in_chart: bool = False
    native_link: str | None = Field(default=None, description="Link to the native OpenEMR page for this record.")

    @model_validator(mode="after")
    def _citation_and_provenance_match_tier(self) -> BriefingLine:
        if self.tier is AssertionTier.GUIDELINE_SUPPORTED:
            raise ValueError("a guideline claim is a Consideration, not a line under a record heading")
        if self.tier is AssertionTier.CHART_FACT and self.record_citation is None:
            raise ValueError("a chart fact must cite the record it came from")
        if self.tier in (AssertionTier.DOCUMENT_STATED, AssertionTier.PATIENT_REPORTED, AssertionTier.COMPUTED):
            if self.document_citation is None:
                raise ValueError(f"a {self.tier.value} line must cite the document it was read from")
        if self.tier is AssertionTier.COMPUTED:
            # The whole of W2-AMB-055 in three lines: a computed comparison shows
            # its inputs, and says it was derived, or it is not displayed at all.
            if self.computed is None:
                raise ValueError("a computed line must show the inputs it was derived from")
            if self.abnormal_flag_source is not AbnormalFlagSource.DERIVED:
                raise ValueError("a computed line must record that the comparison was derived, not printed")
        elif self.computed is not None:
            raise ValueError("only a computed line carries a comparison")
        if self.abnormal_flag_source is AbnormalFlagSource.EXTRACTED and self.tier is not AssertionTier.DOCUMENT_STATED:
            raise ValueError("a printed flag is document-stated")
        return self


class Consideration(StrictModel):
    """A displayed consideration: relevant to *this* patient, and quoted, never instructed.

    Carries the four things F11 §3.3 requires of every one of them - patient-
    specific relevance, the cited patient fact, the attributed guideline
    section, and the uncertainty bearing on it.
    """

    consideration_id: str = Field(min_length=1)
    tier: Literal[AssertionTier.GUIDELINE_SUPPORTED] = AssertionTier.GUIDELINE_SUPPORTED
    topic: str = Field(min_length=1)
    claim_kind: ClaimKind
    text: str = Field(min_length=1)
    relevance: str = Field(min_length=1, description="Why this patient, not patients in general.")
    facts: tuple[CitedFact, ...] = Field(min_length=1)
    citations: tuple[GuidelineCitation, ...] = Field(min_length=1)
    uncertainty: str | None = None

    @model_validator(mode="after")
    def _support_is_admissible(self) -> Consideration:
        # Defence in depth: screening already dropped these, and a Consideration
        # constructed by hand must not be able to route around it.
        for citation in self.citations:
            if not supports(citation.evidence_tier, self.claim_kind):
                raise ValueError(
                    f"{citation.evidence_tier.label} evidence cannot support a {self.claim_kind.value} claim (F07 §3.7)"
                )
        return self


class DroppedAssertion(StrictModel):
    """One claim withheld before display.

    ``detail`` is a fixed, non-clinical explanation: the withheld text never
    travels with the record of its withholding.
    """

    claim_id: str = Field(min_length=1)
    claim_kind: ClaimKind | None = None
    reason: DropReason
    detail: str = Field(min_length=1)


class BriefingSection(StrictModel):
    """One of the three headings, present whether or not it has content."""

    heading: str = Field(min_length=1)
    lines: tuple[BriefingLine, ...] = ()
    considerations: tuple[Consideration, ...] = ()
    note: str | None = Field(default=None, description="Set to NOTHING_TO_REPORT when the section is empty.")

    @property
    def is_empty(self) -> bool:
        return not self.lines and not self.considerations


class Briefing(StrictModel):
    """The whole glanceable overview, plus what was withheld and why."""

    what_changed: tuple[BriefingLine, ...] = ()
    needs_attention: tuple[BriefingLine, ...] = ()
    what_to_consider: tuple[Consideration, ...] = ()
    limitations: tuple[str, ...] = ()
    dropped: tuple[DroppedAssertion, ...] = ()
    refusal: str | None = Field(default=None, description="One of the two fixed sentences; never model prose.")
    corpus_version: str | None = None

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    @property
    def nothing_to_report(self) -> bool:
        return not (self.what_changed or self.needs_attention or self.what_to_consider)

    def sections(self) -> tuple[BriefingSection, ...]:
        """The three headings in order, each with its content or its note."""
        built = (
            BriefingSection(heading=HEADINGS[0], lines=self.what_changed),
            BriefingSection(heading=HEADINGS[1], lines=self.needs_attention),
            BriefingSection(heading=HEADINGS[2], considerations=self.what_to_consider),
        )
        return tuple(
            section.model_copy(update={"note": NOTHING_TO_REPORT}) if section.is_empty else section
            for section in built
        )


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


class ChartFact(StrictModel):
    """A value already in the record, as of a date, with the record citation."""

    test_name: str = Field(min_length=1)
    value: str = Field(min_length=1)
    unit: str | None = None
    observed_on: date | None = None
    citation: RecordCitation


class PatientReport(StrictModel):
    """Something the patient stated that disagrees with the chart (F11 §3.5).

    Surfaced as an observation that two records disagree - with both sources and
    a link to the native page - never as a settled clinical fact, and never as a
    reason to change a curated list.
    """

    text: str = Field(min_length=1)
    document_citation: DocumentCitation
    chart_citation: RecordCitation | None = None
    native_link: str | None = None


class AgedDocument(StrictModel):
    """A stored document too old to brief as new facts (``UNREVIEWED_MAX_AGE_MONTHS``).

    It becomes one Needs attention line saying it holds values nobody has
    reviewed, citing it; its values are not briefed.
    """

    document_id: int = Field(ge=1)
    dated: date
    date_kind: Literal["collected", "received"]
    value_count: int = Field(ge=0)
    citation: DocumentCitation | None = Field(default=None, description="The document's first cited value; None when it has none.")


def _aged_lines(aged: Sequence[AgedDocument]) -> list[BriefingLine]:
    return [
        BriefingLine(
            line_id=f"aged-{doc.document_id}",
            tier=AssertionTier.DOCUMENT_STATED,
            text=(
                f"An older document ({doc.date_kind} {doc.dated.isoformat()}) has "
                f"{doc.value_count} value(s) nobody has reviewed."
            ),
            document_citation=doc.citation,
            not_yet_in_chart=True,
        )
        for doc in aged
        if doc.citation is not None and doc.value_count > 0  # nothing waiting: nothing to say
    ]


class ConsiderationCandidate(StrictModel):
    """A proposed consideration, before screening.

    ``supporting_chunk_ids`` name passages in the :class:`EvidencePackage`. An
    empty tuple means the corpus offered nothing on this topic, which is
    reported as a named limitation rather than as silence.
    """

    consideration_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    claim_kind: ClaimKind
    text: str = Field(min_length=1)
    relevance: str = Field(min_length=1)
    facts: tuple[CitedFact, ...] = Field(min_length=1)
    supporting_chunk_ids: tuple[str, ...] = ()
    uncertainty: str | None = None


# --------------------------------------------------------------------------- #
# Screens
# --------------------------------------------------------------------------- #


def refusal_for(question: str | None) -> str | None:
    """The fixed refusal a question earns, or ``None`` when it may be answered.

    Detection and wording both come from Week 1: the deny-list decides whether
    this is a request for advice, and :func:`app.verifier.canonical_refusal`
    chooses between the two fixed sentences. Nothing here is model prose, and
    the question itself is never echoed back.
    """
    if not question:
        return None
    if _RECOMMENDATION.search(question) or _ADVICE_TOPIC.search(question):
        return canonical_refusal(question)
    return None


def _numbers_in(text: str) -> set[str]:
    return {_normalize_number(token) for token in _NUMBER.findall(text)}


def _numbers_supported(text: str, sources: Iterable[str]) -> bool:
    """Whether every numeric token in ``text`` occurs in one of its cited sources.

    The same check Week 1 applies to a follow-up statement, pointed at quoted
    passages and cited patient facts instead of record fields: a number that is
    in neither is an invention, and the statement carrying it is dropped.
    """
    supported: set[str] = set()
    for source in sources:
        supported |= _numbers_in(source)
    return _numbers_in(text) <= supported


def _directive(*texts: str | None) -> bool:
    """Whether any system-authored text is written in the directive voice.

    Applied to what the system says, never to an attributed quote: a guideline
    may instruct, and we may quote it saying so, but the system's own sentence
    may not.
    """
    return any(text and _RECOMMENDATION.search(text) for text in texts)


def _screen(
    candidates: Sequence[ConsiderationCandidate],
    snippets: dict[str, EvidenceSnippet],
) -> tuple[list[Consideration], list[DroppedAssertion], list[str]]:
    """Apply F11 §4 and F07 §3.7 to candidate considerations, before display.

    Returns the considerations that survive, the drops (each counted, with a
    non-clinical reason), and the corpus limitations to state.
    """
    kept: list[Consideration] = []
    dropped: list[DroppedAssertion] = []
    limitations: list[str] = []

    for candidate in candidates:
        if _directive(candidate.text, candidate.relevance, candidate.uncertainty):
            dropped.append(
                DroppedAssertion(
                    claim_id=candidate.consideration_id,
                    claim_kind=candidate.claim_kind,
                    reason=DropReason.DIRECTIVE_LANGUAGE,
                    detail="the statement was written as an instruction rather than as evidence",
                )
            )
            continue

        if not candidate.supporting_chunk_ids:
            # Nothing was retrieved for this topic. Name the gap (F11 §3.4) and
            # carry on: a gap in one topic never suppresses the others.
            limitations.append(uncovered_topic_limitation(candidate.topic))
            dropped.append(
                DroppedAssertion(
                    claim_id=candidate.consideration_id,
                    claim_kind=candidate.claim_kind,
                    reason=DropReason.UNRESOLVABLE_CITATION,
                    detail="the selected corpus offered no supporting passage for this topic",
                )
            )
            continue

        resolved = [snippets[cid] for cid in candidate.supporting_chunk_ids if cid in snippets]
        if len(resolved) != len(candidate.supporting_chunk_ids):
            # A dangling identifier is a system fault, not a corpus gap, so it
            # is counted but states no limitation about coverage.
            dropped.append(
                DroppedAssertion(
                    claim_id=candidate.consideration_id,
                    claim_kind=candidate.claim_kind,
                    reason=DropReason.UNRESOLVABLE_CITATION,
                    detail="a supporting passage did not resolve in the evidence package",
                )
            )
            continue

        admissible = [s for s in resolved if supports(s.citation.evidence_tier, candidate.claim_kind)]
        if not admissible:
            tiers = "/".join(sorted({s.citation.evidence_tier.label for s in resolved}))
            dropped.append(
                DroppedAssertion(
                    claim_id=candidate.consideration_id,
                    claim_kind=candidate.claim_kind,
                    reason=DropReason.TIER_INADMISSIBLE,
                    detail=f"a {candidate.claim_kind.value} claim cannot rest on {tiers} evidence alone (F07 §3.7)",
                )
            )
            continue

        sources = [s.citation.quote_or_value for s in admissible] + [f.text for f in candidate.facts]
        if not (_numbers_supported(candidate.text, sources) and _numbers_supported(candidate.relevance, sources)):
            dropped.append(
                DroppedAssertion(
                    claim_id=candidate.consideration_id,
                    claim_kind=candidate.claim_kind,
                    reason=DropReason.NUMERIC_UNSUPPORTED,
                    detail="a number in the statement does not appear in the source it cites",
                )
            )
            continue

        kept.append(
            Consideration(
                consideration_id=candidate.consideration_id,
                topic=candidate.topic,
                claim_kind=candidate.claim_kind,
                text=candidate.text,
                relevance=candidate.relevance,
                facts=candidate.facts,
                citations=tuple(s.citation for s in admissible),
                uncertainty=candidate.uncertainty,
            )
        )

    return kept, dropped, limitations


# --------------------------------------------------------------------------- #
# Record-derived headings
# --------------------------------------------------------------------------- #


def _value_text(value: object, unit: str | None) -> str:
    return f"{value} {unit}".strip() if unit else str(value)


def _what_changed(
    document: LabDocument | None,
    prior_facts: Sequence[ChartFact],
    *,
    not_yet_in_chart: bool,
) -> list[BriefingLine]:
    """Newly read values that differ from the prior chart value, each dated."""
    if document is None:
        return []
    prior = {fact.test_name.casefold(): fact for fact in prior_facts}
    lines: list[BriefingLine] = []
    for index, result in enumerate(document.results):
        if result.value is None:
            continue  # an unreadable region is named under Needs attention, never as a change
        observed = result.collection_date or document.collection_date
        current = _value_text(result.value, result.unit)
        previous = prior.get(result.test_name.casefold())
        if previous is None:
            text = f"{result.test_name} {current}{_on(observed)} appears in this document; the supplied records hold no earlier value."
        elif previous.value == str(result.value):
            continue  # unchanged is not a change
        else:
            text = (
                f"{result.test_name} {current}{_on(observed)}; "
                f"the chart value was {_value_text(previous.value, previous.unit)}{_on(previous.observed_on)}."
            )
        lines.append(
            BriefingLine(
                line_id=f"changed-{index}",
                tier=AssertionTier.DOCUMENT_STATED,
                text=text,
                document_citation=result.citation,
                record_citation=previous.citation if previous is not None else None,
                not_yet_in_chart=not_yet_in_chart,
            )
        )
    return lines


def _on(when: date | None) -> str:
    return f" on {when.isoformat()}" if when is not None else ""


def _attention_line(result: LabResult, index: int, *, not_yet_in_chart: bool) -> BriefingLine | None:
    """The one place the printed/computed distinction is decided.

    A flag the lab printed stays document-stated and is quoted as printed. A
    comparison this system made becomes a computed line carrying its inputs.
    Neither can be rendered as the other (:data:`PRINTED_FLAG_LABEL`,
    :data:`COMPUTED_LABEL`).
    """
    if result.verification_status is VerificationStatus.UNREADABLE:
        return BriefingLine(
            line_id=f"attention-{index}",
            tier=AssertionTier.DOCUMENT_STATED,
            text=f"{result.test_name} could not be read from the document and is reported here as unreadable.",
            document_citation=result.citation,
            not_yet_in_chart=not_yet_in_chart,
        )

    if result.abnormal_flag_source is AbnormalFlagSource.EXTRACTED and result.abnormal_flag is not None:
        if result.abnormal_flag is AbnormalFlag.NORMAL:
            return None
        return BriefingLine(
            line_id=f"attention-{index}",
            tier=AssertionTier.DOCUMENT_STATED,
            text=f"{result.test_name} {_value_text(result.value, result.unit)} carries the abnormality printed on the report.",
            document_citation=result.citation,
            abnormal_flag=result.abnormal_flag,
            abnormal_flag_source=AbnormalFlagSource.EXTRACTED,
            not_yet_in_chart=not_yet_in_chart,
        )

    # Not printed: compare here, deterministically, and say so. `derive_abnormal_flag`
    # is the extractor's own rule, reused so the two can never disagree.
    if result.reference_range is None or parse_reference_range(result.reference_range) is None:
        return None
    flag = derive_abnormal_flag(result.value, result.reference_range)
    if flag not in (AbnormalFlag.HIGH, AbnormalFlag.LOW):
        return None
    direction: Literal["above", "below"] = "above" if flag is AbnormalFlag.HIGH else "below"
    return BriefingLine(
        line_id=f"attention-{index}",
        tier=AssertionTier.COMPUTED,
        text=(
            f"{result.test_name} {_value_text(result.value, result.unit)} is {direction} "
            f"the reference range printed on the document ({result.reference_range})."
        ),
        document_citation=result.citation,
        computed=ComputedComparison(
            test_name=result.test_name,
            value=str(result.value),
            unit=result.unit,
            reference_range=result.reference_range,
            direction=direction,
        ),
        abnormal_flag=flag,
        abnormal_flag_source=AbnormalFlagSource.DERIVED,
        not_yet_in_chart=not_yet_in_chart,
    )


def _needs_attention(
    document: LabDocument | None,
    patient_reports: Sequence[PatientReport],
    *,
    not_yet_in_chart: bool,
) -> list[BriefingLine]:
    lines: list[BriefingLine] = []
    if document is not None:
        for index, result in enumerate(document.results):
            line = _attention_line(result, index, not_yet_in_chart=not_yet_in_chart)
            if line is not None:
                lines.append(line)
    for index, report in enumerate(patient_reports):
        lines.append(
            BriefingLine(
                line_id=f"reported-{index}",
                tier=AssertionTier.PATIENT_REPORTED,
                text=report.text,
                document_citation=report.document_citation,
                record_citation=report.chart_citation,
                native_link=report.native_link,
            )
        )
    return lines


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Intake forms: what the patient reported (ADR-010)
# --------------------------------------------------------------------------- #

_VERIFIED_STATUSES = (VerificationStatus.VERIFIED_EXACT, VerificationStatus.VERIFIED_FUZZY)

#: Said when the module sent no chart medication list (an older module, or the
#: medications source could not be read): nothing was compared, and that is stated.
REPORTED_MEDICATIONS_NOT_COMPARED = (
    "Patient-reported medications were not compared with the chart's medication list in this briefing; "
    "check them against the chart before relying on either."
)

#: Said when they were compared. A chart medication the patient did not list is
#: deliberately not flagged: partial forms are common (ADR-010).
REPORTED_MEDICATIONS_COMPARED = (
    "Patient-reported medications were compared with the chart's current medication list by drug name, "
    "and by dose and frequency where both state them; a chart medication the patient did not list is not flagged."
)


def documents_not_included_limitation(count: int) -> str:
    """Said when the module's 20-document cap left documents with values still waiting out of the briefing."""
    return (
        f"{count} older document(s) with values not yet reviewed were not included in this briefing; "
        "review them in the document list."
    )


def blank_section_limitation(section: str, document_id: int) -> str:
    return (
        f"The intake form (document {document_id}) lists no {section} and no written \"none\"; "
        f"a blank section is not a statement of no known {section}."
    )


# Dose and frequency comparison: deliberately narrow. Numbers are compared as
# written (no unit conversion), and a frequency only when both sides use one of
# these phrasings. Anything outside them is not compared rather than guessed.
_DOSE_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_FREQUENCIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("4/day", re.compile(r"\b(qid|four times (a |per )?day|four times daily|4 times (a |per )?day|4x daily)\b")),
    ("3/day", re.compile(r"\b(tid|three times (a |per )?day|three times daily|3 times (a |per )?day|3x daily)\b")),
    ("2/day", re.compile(r"\b(bid|twice (a |per )?day|twice daily|two times (a |per )?day|2 times (a |per )?day|2x daily|every 12 hours|q12h)\b")),
    ("weekly", re.compile(r"\b(weekly|once (a |per )?week|every week)\b")),
    ("1/day", re.compile(r"\b(daily|once (a |per )?day|once daily|every day|qd|nightly|at bedtime|qhs|every morning|every evening|each morning|each night)\b")),
)


def _dose_numbers(text: str | None) -> set[str]:
    return {n.rstrip("0").rstrip(".") if "." in n else n for n in _DOSE_NUMBER.findall(text or "")}


def _frequency(text: str | None) -> str | None:
    lowered = (text or "").lower()
    for code, pattern in _FREQUENCIES:
        if pattern.search(lowered):
            return code
    return None


def _chart_text(record: MedicationRecord) -> str:
    return " ".join(p for p in (record.drug_name, record.dosage_text) if p)


def _differs(reported: ReportedMedication, record: MedicationRecord) -> str | None:
    """What differs between a reported medication and one chart record: "dose", "frequency", or None.

    Only a difference both sides state counts; a side that says nothing is not a disagreement.
    """
    reported_dose = _dose_numbers(reported.dose)
    chart_dose = _dose_numbers(record.drug_name) | _dose_numbers(record.dosage_text)
    if reported_dose and chart_dose and not reported_dose <= chart_dose:
        return "dose"
    reported_freq, chart_freq = _frequency(reported.frequency), _frequency(_chart_text(record))
    if reported_freq and chart_freq and reported_freq != chart_freq:
        return "frequency"
    return None


def _medication_conflicts(form: IntakeForm, chart: Sequence[MedicationRecord]) -> list[BriefingLine]:
    """Reported medications that disagree with the chart's current list (ADR-010).

    Two kinds, both patient-reported lines under Needs attention citing the form item:
    a drug not on the current list (which says the list was checked), and the same
    drug at a different dose or frequency (which also cites the chart record).
    Nothing is written anywhere; the chart is only read.
    """
    # Current = not known to be inactive. An entry whose status fields disagree
    # (active=None) still counts as on the list: flagging it "missing" would be a false alarm.
    active = [r for r in chart if r.active is not False]
    items = [i for i in intake_items(form) if i.section == "medication" and i.label == LABEL_MEDICATION]
    lines: list[BriefingLine] = []
    for med, item in zip(form.current_medications, items, strict=True):
        if med.verification_status is VerificationStatus.UNREADABLE or not (med.name or "").strip():
            continue  # an unreadable entry is already named as unreadable; it names no drug to compare
        text = medication_text(med)
        matches = records_named(med.name or "", active)
        line_id = f"medication-conflict-{form.document_id}-{item.index}"
        if not matches:
            if not active:
                checked = "the chart medication list was checked and holds no current entries"
            elif len(active) == 1:
                checked = "the chart's 1 current medication entry was checked"
            else:
                checked = f"the chart's {len(active)} current medication entries were checked"
            lines.append(BriefingLine(
                line_id=line_id,
                tier=AssertionTier.PATIENT_REPORTED,
                text=f"Patient reports {text} on the intake form; not on the chart medication list ({checked}).",
                document_citation=item.citation,
                not_yet_in_chart=True,
            ))
            continue
        differing = [(r, what) for r in matches if (what := _differs(med, r)) is not None]
        if len(differing) < len(matches):
            continue  # at least one chart record agrees: nothing to flag
        record, what = min(differing, key=lambda pair: (pair[0].timestamp, pair[0].record_id))
        lines.append(BriefingLine(
            line_id=line_id,
            tier=AssertionTier.PATIENT_REPORTED,
            text=(
                f"Patient reports {text} on the intake form; the chart medication list has "
                f"{_chart_text(record)} - the {what} differs. Shown side by side, not reconciled."
            ),
            document_citation=item.citation,
            record_citation=RecordCitation(record_type=RecordType.MEDICATION, record_id=record.record_id, timestamp=record.timestamp),
            not_yet_in_chart=True,
        ))
    return lines


def _intake_lines(
    forms: Sequence[IntakeForm], chart_medications: Sequence[MedicationRecord] | None = None
) -> tuple[list[BriefingLine], list[BriefingLine], list[str]]:
    """Every reported item as a patient-reported line (readable: What changed; unreadable: Needs attention).

    With the chart's medications, reported medications that disagree with them are
    added under Needs attention; without them, the briefing says nothing was compared.
    """
    changed: list[BriefingLine] = []
    attention: list[BriefingLine] = []
    limitations: list[str] = []
    for form in forms:
        for item in intake_items(form):
            line_id = f"intake-{form.document_id}-{item.index}"
            label = item.label[0].lower() + item.label[1:]
            if item.text is None or item.verification_status is VerificationStatus.UNREADABLE:
                attention.append(BriefingLine(
                    line_id=line_id,
                    tier=AssertionTier.PATIENT_REPORTED,
                    text=f"An intake-form entry ({label}) could not be read and is reported here as unreadable.",
                    document_citation=item.citation,
                    not_yet_in_chart=True,
                ))
                continue
            located = "" if item.verification_status in _VERIFIED_STATUSES else " (not located on the page; unverified)"
            changed.append(BriefingLine(
                line_id=line_id,
                tier=AssertionTier.PATIENT_REPORTED,
                text=f"The patient reported on the intake form ({label}): {item.text}{located}.",
                document_citation=item.citation,
                not_yet_in_chart=True,
            ))
        if not form.allergies and form.allergies_none_stated is None:
            limitations.append(blank_section_limitation("allergies", form.document_id))
        if not form.current_medications and form.medications_none_stated is None:
            limitations.append(blank_section_limitation("medications", form.document_id))
        if form.current_medications:
            if chart_medications is None:
                limitations.append(REPORTED_MEDICATIONS_NOT_COMPARED)
            else:
                attention.extend(_medication_conflicts(form, chart_medications))
                limitations.append(REPORTED_MEDICATIONS_COMPARED)
    return changed, attention, limitations


def build_briefing(
    *,
    document: LabDocument | None,
    evidence: EvidencePackage,
    prior_facts: Sequence[ChartFact] = (),
    considerations: Sequence[ConsiderationCandidate] = (),
    patient_reports: Sequence[PatientReport] = (),
    intake_forms: Sequence[IntakeForm] = (),
    chart_medications: Sequence[MedicationRecord] | None = None,
    question: str | None = None,
    document_reviewed: bool = False,
    documents_not_included: int = 0,
    aged_documents: Sequence[AgedDocument] = (),
) -> Briefing:
    """Assemble the three-heading briefing from record facts and screened evidence.

    ``document`` may be ``None`` when there is no document in play. Values read
    from an unreviewed document are marked *not yet in the chart*
    (``document_reviewed=False``, the default) so the physician can tell at a
    glance which values are part of the record (F11 §3.6).

    ``question``, when it asks for treatment advice or dosing, yields the fixed
    refusal. The record-derived headings still render: a refusal answers the
    question asked, it is not a reason to withhold what the chart says.
    """
    refusal = refusal_for(question)
    not_yet_in_chart = document is not None and not document_reviewed

    what_changed = _what_changed(document, prior_facts, not_yet_in_chart=not_yet_in_chart)
    needs_attention = _needs_attention(document, patient_reports, not_yet_in_chart=not_yet_in_chart)
    reported, reported_attention, intake_limitations = _intake_lines(intake_forms, chart_medications)
    what_changed += reported
    needs_attention += reported_attention
    needs_attention += _aged_lines(aged_documents)

    limitations: list[str] = list(intake_limitations)
    if documents_not_included > 0:
        limitations.append(documents_not_included_limitation(documents_not_included))
    dropped: list[DroppedAssertion] = []
    kept: list[Consideration] = []

    if evidence.status is RetrievalStatus.UNAVAILABLE:
        # Zero guideline claims, and the limitation is stated (F11 §4). No
        # per-candidate drop is counted: nothing was retrieved to screen.
        limitations.append(EVIDENCE_UNAVAILABLE_LIMITATION)
    else:
        if evidence.status is RetrievalStatus.DEGRADED:
            limitations.append(RETRIEVAL_DEGRADED_LIMITATION)
        snippets = {snippet.chunk_id: snippet for snippet in evidence.snippets}
        kept, dropped, topic_limitations = _screen(considerations, snippets)
        limitations.extend(topic_limitations)

    for item in dropped:
        # Observable, and carrying no clinical text: the reason, not the claim.
        log_event("briefing.claim_dropped", claim_id=item.claim_id, reason=item.reason.value)

    briefing = Briefing(
        what_changed=tuple(what_changed),
        needs_attention=tuple(needs_attention),
        what_to_consider=tuple(kept),
        limitations=tuple(dict.fromkeys(limitations)),
        dropped=tuple(dropped),
        refusal=refusal,
        corpus_version=evidence.corpus_version,
    )
    log_event(
        "briefing.built",
        changed_count=len(briefing.what_changed),
        attention_count=len(briefing.needs_attention),
        consideration_count=len(briefing.what_to_consider),
        computed_count=sum(1 for line in briefing.needs_attention if line.tier is AssertionTier.COMPUTED),
        medication_conflict_count=sum(1 for line in briefing.needs_attention if line.line_id.startswith("medication-conflict-")),
        dropped_count=briefing.dropped_count,
        limitation_count=len(briefing.limitations),
        retrieval_status=evidence.status.value,
        refused=refusal is not None,
    )
    return briefing


# --------------------------------------------------------------------------- #
# Rendering
#
# Plain text, never markup (`USER.md` §6). The renderer exists so the tier and
# the citation travel with the claim wherever it is displayed - and so the two
# flag labels can be asserted to be different strings.
# --------------------------------------------------------------------------- #


def _render_document_citation(citation: DocumentCitation) -> str:
    return (
        f"document {citation.source_id}, {citation.page_or_section}, "
        f'{citation.field_or_chunk_id}, as printed: "{citation.quote_or_value}"'
    )


def _render_record_citation(citation: RecordCitation) -> str:
    return f"chart {citation.record_type.value} {citation.record_id} ({citation.timestamp.date().isoformat()})"


def _render_guideline_citation(citation: GuidelineCitation) -> list[str]:
    year = str(citation.publication_year) if citation.publication_year is not None else "undated"
    return [
        f"guideline: {citation.publisher}, {year} - {citation.page_or_section}",
        f'    "{citation.quote_or_value}"',
        f"    population scope: {citation.population_scope} | evidence {citation.evidence_tier.label}"
        f" | corpus {citation.corpus_version}",
    ]


def _render_line(line: BriefingLine) -> list[str]:
    out = [f"  - [{line.tier.label}] {line.text}"]
    if line.not_yet_in_chart:
        out.append(f"      {NOT_YET_IN_CHART}")
    if line.abnormal_flag_source is AbnormalFlagSource.EXTRACTED and line.abnormal_flag is not None:
        out.append(f"      {PRINTED_FLAG_LABEL}: {line.abnormal_flag.value}")
    if line.computed is not None:
        out.append(
            f"      {COMPUTED_LABEL}: {_value_text(line.computed.value, line.computed.unit)} is "
            f"{line.computed.direction} the printed reference range {line.computed.reference_range}"
        )
        out.append(f"      rule: {line.computed.rule}")
    if line.document_citation is not None:
        out.append(f"      {_render_document_citation(line.document_citation)}")
    if line.record_citation is not None:
        out.append(f"      {_render_record_citation(line.record_citation)}")
    if line.native_link is not None:
        out.append(f"      open in OpenEMR: {line.native_link}")
    return out


def _render_consideration(consideration: Consideration) -> list[str]:
    out = [f"  - [{consideration.tier.label}] {consideration.text}"]
    out.append(f"      why this patient: {consideration.relevance}")
    for fact in consideration.facts:
        suffix = f" - {NOT_YET_IN_CHART}" if fact.not_yet_in_chart else ""
        out.append(f"      patient fact [{fact.tier.label}]: {fact.text}{suffix}")
        if fact.record_citation is not None:
            out.append(f"          {_render_record_citation(fact.record_citation)}")
        if fact.document_citation is not None:
            out.append(f"          {_render_document_citation(fact.document_citation)}")
    for citation in consideration.citations:
        out.extend(f"      {row}" for row in _render_guideline_citation(citation))
    if consideration.uncertainty:
        out.append(f"      uncertainty: {consideration.uncertainty}")
    return out


def render_briefing(briefing: Briefing) -> str:
    """The briefing as plain text: three headings, tiers, citations, limitations.

    Withheld claims are reported as counts and reasons only - the text that was
    dropped is never printed beside the record of its dropping.
    """
    out: list[str] = []
    if briefing.refusal is not None:
        out.append(briefing.refusal)
        out.append("")
    if briefing.nothing_to_report:
        out.append(NOTHING_TO_REPORT)
        out.append("")

    for section in briefing.sections():
        out.append(section.heading)
        if section.note is not None:
            out.append(f"  {section.note}")
        for line in section.lines:
            out.extend(_render_line(line))
        for consideration in section.considerations:
            out.extend(_render_consideration(consideration))
        out.append("")

    if briefing.limitations:
        out.append("Evidence limitations")
        out.extend(f"  - {limitation}" for limitation in briefing.limitations)
        out.append("")

    if briefing.dropped:
        reasons = ", ".join(sorted({item.reason.value for item in briefing.dropped}))
        out.append(f"Withheld before display: {briefing.dropped_count} ({reasons})")

    return "\n".join(out).rstrip() + "\n"


__all__ = [
    "AgedDocument",
    "COMPARISON_RULE",
    "COMPUTED_LABEL",
    "EVIDENCE_UNAVAILABLE_LIMITATION",
    "HEADINGS",
    "NOTHING_TO_REPORT",
    "NOT_YET_IN_CHART",
    "PRINTED_FLAG_LABEL",
    "RETRIEVAL_DEGRADED_LIMITATION",
    "AssertionTier",
    "Briefing",
    "BriefingLine",
    "BriefingSection",
    "ChartFact",
    "CitedFact",
    "ComputedComparison",
    "Consideration",
    "ConsiderationCandidate",
    "DropReason",
    "DroppedAssertion",
    "PatientReport",
    "build_briefing",
    "refusal_for",
    "render_briefing",
    "uncovered_topic_limitation",
]
