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

import asyncio
import base64
import binascii
import re
from datetime import date
from enum import StrEnum
from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.briefing import (
    AssertionTier,
    Briefing,
    ChartFact,
    CitedFact,
    ConsiderationCandidate,
    build_briefing,
    render_briefing,
)
from app.contracts import Citation, LabResult as ChartLabResult, RecordType, StrictModel
from app.corpus import ClaimKind
from app.documents import SUPPORTED_MEDIA_TYPES, ExtractionMetadata, LabDocument, LabResult, MediaType, VerificationStatus
from app.evidence import EvidencePackage, RetrievalQuery, RetrievalStatus
from app.lab_extractor import extract_lab_document
from app.providers.prompt import LAB_EXTRACTION_PROMPT_VERSION
from app.observability import generation, log_event, score, span
from app.providers.base import ContentPart, ModelProvider, ProviderError, TextPart
from app.providers.stub_provider import StubProvider
from app.reranker import BedrockReranker, FakeReranker
from app.retrieval import HybridRetriever, build_retriever


# Largest stored file the agent will read: well above any real lab report or
# intake form, well below the model's 32 MB request limit. The PHP module checks
# the same number before encoding, so an oversized file never leaves OpenEMR.
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BASE64_CHARS = 4 * -(-MAX_DOCUMENT_BYTES // 3)


# --------------------------------------------------------------------------- #
# HTTP contract - PHP module -> agent
# --------------------------------------------------------------------------- #


#: Most stored documents one briefing accepts. A bound on work and body size,
#: well above one patient's unreviewed lab reports.
MAX_BRIEFING_DOCUMENTS = 20
#: Most chart lab results one briefing accepts as prior facts.
MAX_PRIOR_FACTS = 500


class StoredDocument(StrictModel):
    """One document's stored extraction, as the module saved it from ``/v1/documents/extract``.

    Fails closed on attribution: the extraction and every citation in it must
    name the listed ``document_id``, or the briefing would cite the wrong file.
    """

    document_id: int = Field(ge=1)
    doc_type: Literal["lab_pdf"]
    extraction: LabDocument

    @model_validator(mode="after")
    def _attributed_to_this_document(self) -> StoredDocument:
        if self.extraction.document_id != self.document_id:
            raise ValueError("the stored extraction belongs to a different document_id")
        if any(r.citation.source_id != str(self.document_id) for r in self.extraction.results):
            raise ValueError("a stored citation names a different document")
        return self


class DocumentBriefingRequest(StrictModel):
    """Signed body of ``POST /v1/documents/briefing``.

    Signed with the same HMAC scheme as ``POST /v1/bundles``
    (``X-Copilot-Signature`` + ``X-Copilot-Timestamp`` over the raw body), so
    the module reuses ``BundleSigner`` and ``GuzzleAgentClient`` unchanged.

    Two ways in, exactly one per request (contract C4):

    - ``documents``: the stored extractions of every extracted document for
      this patient. Nothing is re-read; the supervisor goes straight to
      evidence retrieval, and each citation names its own document.
    - ``document_id`` + ``media_type`` + ``document_base64``: the legacy
      single-document path, extracted in-request. Kept for one release.

    ``prior_facts`` is the chart's lab history, the same shape as the Week 1
    bundle's ``lab_results``; it turns a new value into a dated change.

    ``patient_uuid`` never ``pid`` - the same rule as the Week 1 bundle.
    """

    correlation_id: UUID
    patient_uuid: UUID
    documents: list[StoredDocument] | None = Field(default=None, min_length=1, max_length=MAX_BRIEFING_DOCUMENTS)
    prior_facts: list[ChartLabResult] = Field(default_factory=list, max_length=MAX_PRIOR_FACTS)
    document_id: int | None = Field(default=None, ge=1)
    media_type: MediaType | None = None
    document_base64: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_DOCUMENT_BASE64_CHARS,
        description="Legacy path: the stored file's bytes, at most MAX_DOCUMENT_BYTES decoded. Never logged.",
    )
    question: str | None = Field(
        default=None,
        max_length=500,
        description="Optional physician question. A request for advice or dosing earns the fixed refusal.",
    )

    @model_validator(mode="after")
    def _exactly_one_path(self) -> DocumentBriefingRequest:
        legacy = (self.document_id, self.media_type, self.document_base64)
        if self.documents is not None:
            if any(v is not None for v in legacy):
                raise ValueError("send either documents or document_id/media_type/document_base64, not both")
            ids = [d.document_id for d in self.documents]
            if len(set(ids)) != len(ids):
                raise ValueError("a document is listed more than once")
        elif any(v is None for v in legacy):
            raise ValueError("send documents, or all of document_id, media_type and document_base64")
        return self

    @property
    def document_ids(self) -> tuple[int, ...]:
        if self.documents is not None:
            return tuple(d.document_id for d in self.documents)
        assert self.document_id is not None
        return (self.document_id,)


# --------------------------------------------------------------------------- #
# HTTP contract - extraction only (contract C4, ADR-012)
# --------------------------------------------------------------------------- #

#: Document types the module sends, from the OpenEMR category (ADR-012). Only
#: ``lab_pdf`` is extracted in Wave 1; ``intake_form`` is accepted and answered
#: with a fixed "not supported yet" code so the module can record it.
DocType = Literal["lab_pdf", "intake_form"]

DOC_TYPE_NOT_SUPPORTED_YET = "doc_type_not_supported_yet"


class DocumentExtractRequest(StrictModel):
    """Signed body of ``POST /v1/documents/extract``: one stored document, extraction only.

    Signed like every module -> agent call. The module stores the returned
    extraction on its processing record and sends it back to the briefing
    route, so a document is read by the model once per (content, prompt version).
    """

    correlation_id: UUID
    patient_uuid: UUID
    document_id: int = Field(ge=1)
    doc_type: DocType
    media_type: MediaType
    document_base64: str = Field(
        min_length=1,
        max_length=MAX_DOCUMENT_BASE64_CHARS,
        description="The stored file's bytes, at most MAX_DOCUMENT_BYTES decoded. Never logged.",
    )


class PrintedIdentity(StrictModel):
    """Name and date of birth as printed on the document (contract C2).

    Returned to the module only, which compares them with the chart (ADR-012).
    Never logged, never traced, never shown in a briefing.
    """

    name: str | None = None
    dob: date | None = None


class DocumentExtractResponse(StrictModel):
    correlation_id: UUID
    patient_uuid: UUID
    document_id: int
    doc_type: DocType
    status: Literal["ok", "degraded"] = "ok"
    degraded_reason: str | None = Field(
        default=None, description="Fixed string. Never a raw exception, never patient-specific."
    )
    prompt_version: str = Field(description='The extraction prompt version the result is cached under; "none" when no extractor ran.')
    extraction_model: str = Field(description='The model that produced the extraction; "none" when there is none.')
    extraction: LabDocument | None = None
    printed_identity: PrintedIdentity | None = None


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


class RoutingDecision(StrictModel):
    """One supervisor handoff (CR4: handoffs are logged). Codes only - no content."""

    step: int = Field(ge=1)
    source: Literal["supervisor"]
    target: Literal["intake-extractor", "evidence-retriever", "answer", "finish"]
    reason_code: Literal[
        "document_pending_extraction",
        "evidence_required",
        "evidence_ready",
        "briefing_complete",
        "worker_failed",
        "budget_exhausted",
        "iteration_limit",
    ]
    doc_type: Literal["lab_pdf", "intake_form"]


class DocumentBriefingResponse(StrictModel):
    """Everything the panel renders.

    ``status="degraded"`` still returns 200 with whatever could be produced and
    a fixed ``degraded_reason`` - the same contract as the Week 1 briefing, so
    a failure is stated rather than surfacing as a broken panel.
    """

    correlation_id: UUID
    patient_uuid: UUID
    document_id: int = Field(description="The first document briefed; kept for callers that read one id. See document_ids.")
    document_ids: tuple[int, ...] = Field(default=(), description="Every document this briefing covers, in request order.")
    status: BriefingStatus = BriefingStatus.OK
    degraded_reason: str | None = Field(
        default=None, description="Fixed string. Never a raw exception, never patient-specific."
    )
    routing: tuple[RoutingDecision, ...] = Field(
        default=(), description="The supervisor's handoffs, in order - the path this briefing took."
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
- Attribute the guidance by describing what it says: "The guideline states...", "The guideline describes...", "NDEP notes...", "The guidance reports...". Never tell the physician what to do. Never recommend a drug, a dose, or a treatment change.
- Do not use these words anywhere in text, relevance or uncertainty, even when attributing them to the guideline: should, must, recommend, recommends, recommended, advise, advised, suggest, suggests, consider, need to, needs to, ought to. A safety filter cannot tell attribution from instruction and discards any statement containing them. Paraphrase instead: "the guideline states that X is individualised", not "the guideline recommends individualising X".
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


def combine_documents(documents: list[LabDocument]) -> LabDocument:
    """Several stored extractions as the one ``LabDocument`` the briefing reads.

    Every result keeps its own citation, so each line still names the document
    it came from. A document-level collection date is copied onto its results
    first, because the combined document can carry no single date. Metadata is
    recomputed from the results, as ``summarize`` does for one document.
    """
    if len(documents) == 1:
        return documents[0]
    results: list[LabResult] = []
    for doc in documents:
        for r in doc.results:
            if r.collection_date is None and doc.collection_date is not None:
                r = r.model_copy(update={"collection_date": doc.collection_date})
            results.append(r)
    metas = [d.extraction_metadata for d in documents]
    verified = sum(
        1 for r in results if r.verification_status in (VerificationStatus.VERIFIED_EXACT, VerificationStatus.VERIFIED_FUZZY)
    )
    return LabDocument(
        document_id=documents[0].document_id,
        results=results,
        extraction_metadata=ExtractionMetadata(
            model_id=",".join(dict.fromkeys(m.model_id for m in metas)),
            prompt_version=",".join(dict.fromkeys(m.prompt_version for m in metas)),
            extracted_at=max(m.extracted_at for m in metas),
            page_count=sum(m.page_count for m in metas),
            verified_fraction=(verified / len(results)) if results else 0.0,
            unreadable_count=sum(1 for r in results if r.verification_status is VerificationStatus.UNREADABLE),
            unverified_count=sum(1 for r in results if r.verification_status is VerificationStatus.UNVERIFIED),
        ),
    )


def chart_facts(prior: list[ChartLabResult]) -> list[ChartFact]:
    """The chart's lab history as briefing facts, oldest first so the newest value per test wins."""
    ordered = sorted(prior, key=lambda r: (r.observed_at, r.result_id))
    return [
        ChartFact(
            test_name=r.test_name,
            value=str(r.value),
            unit=r.units,
            observed_on=r.observed_at.date(),
            citation=Citation(record_type=RecordType.LAB_RESULT, record_id=r.result_id, timestamp=r.observed_at),
        )
        for r in ordered
    ]


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


#: Longest document read (the F02 proposal). Each OCR'd page can cost a Textract call of up to
#: its own timeout, so an unbounded page count would outlast the module's 90 s client timeout.
MAX_DOCUMENT_PAGES = 20

#: Wall-clock budget for reading one document, below the module's 90 s extract timeout, so the
#: agent stops (and stops spending) before the module gives up on the request.
DOCUMENT_EXTRACT_BUDGET_SECONDS = 75.0


def pdf_page_count(raw: bytes) -> int | None:
    """Pages in a PDF (via page_text, under its pdfium lock), or None when it cannot be opened."""
    from app.page_text import pdf_page_count as locked_page_count  # noqa: PLC0415 - page-text stack loads lazily

    return locked_page_count(raw)


async def read_lab_document(
    *,
    document_id: int,
    document_base64: str,
    media_type: str,
    provider: ModelProvider,
    budget_seconds: float | None = None,
) -> tuple[LabDocument | None, str | None]:
    """Decode and extract one lab document. Never raises for a bad file, a model failure or a slow read.

    Returns the document, or ``None`` with a fixed reason code. Shared by the
    extract route and the briefing graph's intake-extractor, so both report the
    same code for the same failure.
    """
    try:
        raw = base64.b64decode(document_base64, validate=True)
    except (binascii.Error, ValueError):
        return None, "document_not_decodable"
    if media_type == "application/pdf":
        pages = pdf_page_count(raw)
        if pages is not None and pages > MAX_DOCUMENT_PAGES:
            return None, "too_many_pages"
    try:
        async with asyncio.timeout(DOCUMENT_EXTRACT_BUDGET_SECONDS if budget_seconds is None else budget_seconds):
            document = await extract_lab_document(
                document_id=document_id,
                pdf_bytes=raw,
                media_type=media_type,
                provider=provider,
            )
    except TimeoutError:
        return None, "budget_exhausted"
    except ProviderError:
        return None, "extraction_unavailable"
    except ValueError:
        return None, "document_not_readable"
    return document, None


def printed_identity_of(document: LabDocument) -> PrintedIdentity | None:
    """The identity printed on the document, if the extractor read one (contract C2).

    Read by attribute so this route works before and after ``LabDocument``
    gains the field. Never logged.
    """
    printed = getattr(document, "printed_identity", None)
    if printed is None:
        return None
    return PrintedIdentity(name=getattr(printed, "name", None), dob=getattr(printed, "dob", None))


async def run_document_extract(request: DocumentExtractRequest, *, provider: ModelProvider) -> DocumentExtractResponse:
    """One extraction = one trace. Dispatches on ``doc_type`` (ADR-012)."""
    base = {
        "correlation_id": request.correlation_id,
        "patient_uuid": request.patient_uuid,
        "document_id": request.document_id,
        "doc_type": request.doc_type,
    }
    with span("document_extract", cid=request.correlation_id, stage=request.doc_type) as attrs:
        if request.doc_type != "lab_pdf":
            response = DocumentExtractResponse(
                **base,
                status="degraded",
                degraded_reason=DOC_TYPE_NOT_SUPPORTED_YET,
                prompt_version="none",
                extraction_model="none",
            )
        else:
            document, reason = await read_lab_document(
                document_id=request.document_id,
                document_base64=request.document_base64,
                media_type=request.media_type,
                provider=provider,
            )
            if document is None:
                response = DocumentExtractResponse(
                    **base,
                    status="degraded",
                    degraded_reason=reason,
                    prompt_version=LAB_EXTRACTION_PROMPT_VERSION,
                    extraction_model="none",
                )
            else:
                response = DocumentExtractResponse(
                    **base,
                    prompt_version=document.extraction_metadata.prompt_version,
                    extraction_model=document.extraction_metadata.model_id,
                    # ADR-012: the printed name/DOB go to the module once, top-level, for its
                    # identity check - never inside the extraction it stores (extraction_json).
                    extraction=document.model_copy(update={"printed_identity": None}),
                    printed_identity=printed_identity_of(document),
                )
                attrs["records"] = len(document.results)
        attrs["outcome"] = response.status
        attrs["reason_code"] = response.degraded_reason
        score("document_extract_degraded", response.status != "ok", data_type="BOOLEAN")
        log_event(
            "document_extract.completed",
            cid=request.correlation_id,
            document_id=request.document_id,
            doc_type=request.doc_type,
            status=response.status,
            reason_code=response.degraded_reason,
            results=len(response.extraction.results) if response.extraction is not None else 0,
        )
        return response


async def run_document_briefing(
    request: DocumentBriefingRequest,
    *,
    provider: ModelProvider,
    reranker: FakeReranker | BedrockReranker,
) -> DocumentBriefingResponse:
    """One document briefing = one trace (CR7: every encounter logged).

    The outermost span for a correlation id opens the Langfuse trace. Inside it
    the supervisor's decisions and each worker - intake-extractor, evidence-
    retriever, answer - are spans, with lab_extract, retrieval, rerank and
    answer_considerations beneath them, so the tool sequence and per-step
    latency read as one encounter.
    The trace id is the correlation id the module already logs, so a panel
    request, its audit row and its trace can be joined.
    """
    # Attribute keys are from TRACE_ALLOWED_KEYS; anything else exports as <masked>.
    with span("document_briefing", cid=request.correlation_id) as attrs:
        response = await _run_document_briefing(request, provider=provider, reranker=reranker)
        attrs["outcome"] = response.status.value
        attrs["reason_code"] = response.degraded_reason
        score("document_briefing_degraded", response.status is not BriefingStatus.OK, data_type="BOOLEAN")
        return response


async def _run_document_briefing(
    request: DocumentBriefingRequest,
    *,
    provider: ModelProvider,
    reranker: FakeReranker | BedrockReranker,
) -> DocumentBriefingResponse:
    """Supervisor + two workers (``app/workflow.py``). Never raises for a model failure."""
    from app.workflow import run_supervised_briefing  # the graph imports this module's contract

    return await run_supervised_briefing(request, provider=provider, reranker=reranker)


__all__ = [
    "ANSWER_SYSTEM_PROMPT",
    "MAX_DOCUMENT_BYTES",
    "SUPPORTED_MEDIA_TYPES",
    "DOC_TYPE_NOT_SUPPORTED_YET",
    "BriefingStatus",
    "DocType",
    "DocumentExtractRequest",
    "DocumentExtractResponse",
    "PrintedIdentity",
    "ConsiderationDraft",
    "ConsiderationDraftSet",
    "DocumentBriefingRequest",
    "DocumentBriefingResponse",
    "Provenance",
    "RoutingDecision",
    "MAX_BRIEFING_DOCUMENTS",
    "MAX_PRIOR_FACTS",
    "StoredDocument",
    "build_query",
    "build_reranker",
    "chart_facts",
    "combine_documents",
    "drafts_to_candidates",
    "get_retriever",
    "printed_identity_of",
    "read_lab_document",
    "run_document_extract",
    "run_document_briefing",
]
