"""Week 2 document briefing: one uploaded lab document in, one grounded briefing out.

This module is the wiring. Every stage already exists and is tested on its own;
this is the first place they are called in sequence from a real request:

    PHP module (signed POST)            <- the document lives in OpenEMR's own
      -> extract_lab_document              ``documents`` table (PRD CR1, D1)
      -> HybridRetriever                 sparse + dense + RRF   (F08)
      -> Reranker                        fake or Bedrock        (F09, ADR-002)
      -> answer model                    proposes considerations from the top
                                         evidence only          (CR3)
      -> build_briefing                  screens every claim    (F11)
      -> DocumentBriefingResponse

The HTTP contract (request and response below) is what the PHP module and the
panel build against. Do not widen it without changing both sides.

The answer model PROPOSES; build_briefing DISPOSES. A consideration the model
invents without a resolvable chunk, with a number absent from its sources, with
a directive, or resting a threshold on Tier B evidence is dropped before
display and counted. That is the Week 1 verification spine, extended to
guideline claims.
"""

from __future__ import annotations

import base64
import binascii
import re
from enum import StrEnum
from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.briefing import (
    AssertionTier,
    Briefing,
    CitedFact,
    ConsiderationCandidate,
    build_briefing,
    render_briefing,
)
from app.contracts import StrictModel
from app.corpus import ClaimKind
from app.documents import LabDocument, LabResult, VerificationStatus
from app.evidence import EvidencePackage, RetrievalQuery, RetrievalStatus
from app.lab_extractor import extract_lab_document
from app.observability import log_event
from app.providers.base import ContentPart, ModelProvider, ProviderError, TextPart
from app.providers.stub_provider import StubProvider
from app.reranker import BedrockReranker, FakeReranker
from app.retrieval import HybridRetriever, build_retriever

SUPPORTED_MEDIA_TYPES = ("application/pdf", "image/png", "image/jpeg")


# --------------------------------------------------------------------------- #
# HTTP contract - PHP module -> agent
# --------------------------------------------------------------------------- #


class DocumentBriefingRequest(StrictModel):
    """Signed body of ``POST /v1/documents/briefing``.

    Signed with the same HMAC scheme as ``POST /v1/bundles``
    (``X-Copilot-Signature`` + ``X-Copilot-Timestamp`` over the raw body), so
    the module reuses ``BundleSigner`` and ``GuzzleAgentClient`` unchanged.

    ``patient_uuid`` never ``pid`` - the same rule as the Week 1 bundle.
    ``document_id`` is OpenEMR's ``documents.id``; it becomes every citation's
    ``source_id``, so click-to-source can resolve back to the stored file.
    """

    correlation_id: UUID
    patient_uuid: UUID
    document_id: int = Field(ge=1)
    media_type: Literal["application/pdf", "image/png", "image/jpeg"]
    document_base64: str = Field(min_length=1, description="The stored file's bytes. Never logged.")
    question: str | None = Field(
        default=None,
        max_length=500,
        description="Optional physician question. A request for advice or dosing earns the fixed refusal.",
    )


# --------------------------------------------------------------------------- #
# HTTP contract - agent -> PHP module -> panel
# --------------------------------------------------------------------------- #


class BriefingStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


class Provenance(StrictModel):
    """What produced this briefing - shown in the panel, not hidden in logs.

    ``reranker`` is displayed so that nothing claims more than it does: a
    deterministic lexical fallback must never read as a learned reranker.
    """

    extraction_model: str
    answer_model: str
    reranker: str
    corpus_version: str
    evidence_status: RetrievalStatus


class DocumentBriefingResponse(StrictModel):
    """Everything the panel renders.

    ``status="degraded"`` still returns 200 with whatever could be produced and
    a fixed ``degraded_reason`` - the same contract as the Week 1 briefing, so
    a failure is stated rather than surfacing as a broken panel.
    """

    correlation_id: UUID
    patient_uuid: UUID
    document_id: int
    status: BriefingStatus = BriefingStatus.OK
    degraded_reason: str | None = Field(
        default=None, description="Fixed string. Never a raw exception, never patient-specific."
    )
    briefing: Briefing | None = None
    rendered_text: str = ""
    provenance: Provenance | None = None


# --------------------------------------------------------------------------- #
# The answer model - proposes considerations from the top evidence only (CR3)
#
# Flat on purpose: strings, lists of strings, one enum-valued string. The strict
# ConsiderationCandidate, its CitedFacts and their document citations are built
# in code from the extraction we already verified - the model names WHICH tests
# a consideration rests on, and never writes a citation itself.
# --------------------------------------------------------------------------- #


class ConsiderationDraft(StrictModel):
    topic: str = Field(min_length=1)
    claim_kind: Literal["threshold", "care_process"] = Field(
        description='"threshold" if it rests on a target, cutoff or decision boundary; otherwise "care_process".'
    )
    text: str = Field(min_length=1, description="What the guidance says, attributed. Never an instruction.")
    relevance: str = Field(min_length=1, description="Why THIS patient, using their results.")
    fact_test_names: list[str] = Field(description="Lab tests, named exactly as in the results, this rests on.")
    supporting_chunk_ids: list[str] = Field(description="Passage ids from the list you were given. At least one.")
    uncertainty: str | None = None


class ConsiderationDraftSet(StrictModel):
    considerations: list[ConsiderationDraft] = Field(default_factory=list)


ANSWER_SYSTEM_PROMPT = """You help a primary care physician prepare for a follow-up visit.

You receive lab results read from a document the patient's record has just received, and numbered guideline passages retrieved for them. Propose considerations: points the guidance makes that bear on THIS patient's results.

Each rule below is enforced by code after you answer. A consideration that breaks one is discarded, so breaking one only loses you the consideration.
- Cite only passage ids you were given, in supporting_chunk_ids. Cite at least one.
- Every number you write must appear in a cited passage or in the lab results. Do not compute, convert or round.
- Attribute the guidance ("The guideline states...", "Guidance recommends individualising..."). Never tell the physician what to do. Never recommend a drug, a dose, or a treatment change.
- claim_kind is "threshold" when the consideration rests on a target, cutoff or decision boundary; otherwise "care_process".
- relevance explains why this patient, from their results - not why patients in general.
- fact_test_names lists the lab tests, named exactly as in the results, the consideration rests on. At least one.
- A result marked UNREADABLE has no known value. Do not state, estimate or infer one.
- If the passages do not bear on these results, return an empty list. That is a correct answer, not a failure."""


ANSWER_MAX_OUTPUT_TOKENS = 4096
QUERY_TOP_K = 5

# Narrow, documented query expansion. The corpus is diabetes guidance and the
# scenario is a lab report, so an HbA1c result is expanded into the topics the
# guidance organises itself around. Named here as query rewriting - an F18
# retrieval improvement - rather than hidden inside the retriever.
_A1C = re.compile(r"\b(a1c|hba1c|hemoglobin a1c|glycated)\b", re.I)
_A1C_EXPANSION = (
    "individualized glycemic target A1C goal",
    "A1C testing frequency monitoring",
    "barriers adherence ability to afford medications",
    "diabetes self-management education and support",
)


def build_query(document: LabDocument) -> str:
    """Minimised clinical concepts: test names and topics. Never values, never identifiers."""
    names = sorted({r.test_name for r in document.results})
    parts = list(names)
    if any(_A1C.search(n) for n in names):
        parts.extend(_A1C_EXPANSION)
    return " ; ".join(parts) or "diabetes follow-up laboratory results"


def _is_unreadable(result: LabResult) -> bool:
    return result.verification_status is VerificationStatus.UNREADABLE or result.value is None


def _fact_text(result: LabResult) -> str:
    if _is_unreadable(result):
        return f"{result.test_name}: value unreadable on the document (printed '{result.citation.quote_or_value}')"
    unit = f" {result.unit}" if result.unit else ""
    rng = f" (printed reference range {result.reference_range})" if result.reference_range else ""
    return f"{result.test_name} {result.value}{unit}{rng}"


def _answer_content(document: LabDocument, evidence: EvidencePackage) -> list[ContentPart]:
    lines = ["<lab_results>"]
    for r in document.results:
        if _is_unreadable(r):
            lines.append(f"- {r.test_name}: UNREADABLE (printed '{r.citation.quote_or_value}')")
        else:
            unit = f" {r.unit}" if r.unit else ""
            rng = f" (reference range {r.reference_range})" if r.reference_range else ""
            lines.append(f"- {r.test_name}: {r.value}{unit}{rng}")
    lines.append("</lab_results>")
    lines.append("<guideline_passages>")
    for sn in evidence.snippets:
        c = sn.citation
        lines.append(f"[{sn.chunk_id}] Tier {c.evidence_tier.value} | {c.publisher} | {c.page_or_section}")
        lines.append(sn.text)
    lines.append("</guideline_passages>")
    return [TextPart(text="\n".join(lines))]


def drafts_to_candidates(drafts: ConsiderationDraftSet, document: LabDocument) -> list[ConsiderationCandidate]:
    """Attach verified patient facts to each draft; the model never writes a citation.

    A draft naming no test we actually extracted cannot carry a patient fact,
    so it cannot become a consideration - ConsiderationCandidate requires one.
    It is logged as dropped, never silently lost.
    """
    by_name = {r.test_name.strip().lower(): r for r in document.results}
    out: list[ConsiderationCandidate] = []
    for i, d in enumerate(drafts.considerations):
        facts = []
        for name in d.fact_test_names:
            r = by_name.get(name.strip().lower())
            if r is not None:
                facts.append(
                    CitedFact(
                        text=_fact_text(r),
                        tier=AssertionTier.DOCUMENT_STATED,
                        document_citation=r.citation,
                        not_yet_in_chart=True,
                    )
                )
        if not facts:
            log_event("document_briefing.draft_dropped", index=i, reason="no_matching_patient_fact")
            continue
        out.append(
            ConsiderationCandidate(
                consideration_id=f"c{i + 1}",
                topic=d.topic,
                claim_kind=ClaimKind(d.claim_kind),
                text=d.text,
                relevance=d.relevance,
                facts=tuple(facts),
                supporting_chunk_ids=tuple(d.supporting_chunk_ids),
                uncertainty=d.uncertainty,
            )
        )
    return out


# Deterministic stand-in so the whole pipeline runs offline in CI. It proposes
# one care-process consideration per passage, resting on the first readable
# result, with no numbers in its own words - so it survives screening without
# needing to be clever, and a test failure means the wiring broke.
_PASSAGE_LINE = re.compile(r"^\[(?P<id>[^\]]+)\] Tier (?P<tier>\w+) \| [^|]+ \| (?P<section>.+)$", re.M)
_RESULT_LINE = re.compile(r"^- (?P<name>[^:]+): (?P<rest>.+)$", re.M)


def _stub_considerations(content: list[ContentPart]) -> ConsiderationDraftSet:
    text = "\n".join(p.text for p in content if isinstance(p, TextPart))
    readable = [m.group("name") for m in _RESULT_LINE.finditer(text) if not m.group("rest").startswith("UNREADABLE")]
    if not readable:
        return ConsiderationDraftSet()
    drafts = [
        ConsiderationDraft(
            topic=m.group("section")[:80],
            claim_kind="care_process",
            text="The guideline section cited here addresses results of this kind.",
            relevance=f"This document reports a {readable[0]} result.",
            fact_test_names=[readable[0]],
            supporting_chunk_ids=[m.group("id")],
        )
        for m in list(_PASSAGE_LINE.finditer(text))[:2]
    ]
    return ConsiderationDraftSet(considerations=drafts)


StubProvider.register_fixture(ConsiderationDraftSet, _stub_considerations)


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def get_retriever() -> HybridRetriever:
    """Built once per process: the corpus is committed content and deterministic."""
    return build_retriever()


def build_reranker(kind: str, *, region: str) -> FakeReranker | BedrockReranker:
    """``fake`` unless Bedrock is explicitly configured.

    The default is deliberately the offline one, so a missing AWS setup never
    turns into a silent network dependency - and provenance says which ran.
    """
    corpus = get_retriever().corpus
    if kind == "bedrock":
        return BedrockReranker(corpus, region=region)
    return FakeReranker(corpus)


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


async def run_document_briefing(
    request: DocumentBriefingRequest,
    *,
    provider: ModelProvider,
    reranker: FakeReranker | BedrockReranker,
) -> DocumentBriefingResponse:
    """Extract -> retrieve -> rerank -> propose -> screen. Never raises for a model failure."""
    base = {
        "correlation_id": request.correlation_id,
        "patient_uuid": request.patient_uuid,
        "document_id": request.document_id,
    }
    try:
        pdf_bytes = base64.b64decode(request.document_base64, validate=True)
    except (binascii.Error, ValueError):
        return DocumentBriefingResponse(**base, status=BriefingStatus.DEGRADED, degraded_reason="document_not_decodable")

    try:
        document = await extract_lab_document(
            document_id=request.document_id,
            pdf_bytes=pdf_bytes,
            media_type=request.media_type,
            provider=provider,
        )
    except ProviderError:
        return DocumentBriefingResponse(**base, status=BriefingStatus.DEGRADED, degraded_reason="extraction_unavailable")
    except ValueError:
        return DocumentBriefingResponse(**base, status=BriefingStatus.DEGRADED, degraded_reason="document_not_readable")

    retriever = get_retriever()
    query = build_query(document)
    retrieved = retriever.retrieve(RetrievalQuery(text=query, corpus_version=retriever.corpus_version))
    evidence = reranker.rerank(query, retrieved.candidates, top_k=QUERY_TOP_K)

    status, reason, answer_model = BriefingStatus.OK, None, "none"
    candidates: list[ConsiderationCandidate] = []
    if evidence.snippets:
        try:
            parsed = await provider.parse_structured(
                system=ANSWER_SYSTEM_PROMPT,
                content=_answer_content(document, evidence),
                schema=ConsiderationDraftSet,
                max_tokens=ANSWER_MAX_OUTPUT_TOKENS,
                effort="low",
            )
            answer_model = parsed.usage.model
            candidates = drafts_to_candidates(parsed.output, document)
        except ProviderError:
            # The record-derived headings still render; only the guidance is missing.
            status, reason = BriefingStatus.DEGRADED, "answer_model_unavailable"

    briefing = build_briefing(
        document=document,
        evidence=evidence,
        considerations=candidates,
        question=request.question,
    )

    log_event(
        "document_briefing.completed",
        cid=request.correlation_id,
        document_id=request.document_id,
        results=len(document.results),
        snippets=len(evidence.snippets),
        considerations_proposed=len(candidates),
        considerations_shown=len(briefing.what_to_consider),
        dropped=len(briefing.dropped),
        evidence_status=evidence.status.value,
    )

    return DocumentBriefingResponse(
        **base,
        status=status,
        degraded_reason=reason,
        briefing=briefing,
        rendered_text=render_briefing(briefing),
        provenance=Provenance(
            extraction_model=document.extraction_metadata.model_id,
            answer_model=answer_model,
            reranker=reranker.model_id,
            corpus_version=retriever.corpus_version,
            evidence_status=evidence.status,
        ),
    )


__all__ = [
    "ANSWER_SYSTEM_PROMPT",
    "SUPPORTED_MEDIA_TYPES",
    "BriefingStatus",
    "ConsiderationDraft",
    "ConsiderationDraftSet",
    "DocumentBriefingRequest",
    "DocumentBriefingResponse",
    "Provenance",
    "build_query",
    "build_reranker",
    "drafts_to_candidates",
    "get_retriever",
    "run_document_briefing",
]
