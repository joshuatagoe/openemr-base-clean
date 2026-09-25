"""Supervisor + two workers for the document briefing (PRD CR4, ADR-001).

This module is the ``WorkflowEngine`` boundary: the only place LangGraph types
appear. Everything a node does is an existing, separately tested function; the
graph decides the order and records why.

    START -> supervisor --document_pending_extraction--> intake-extractor --+
               ^  |  |                                                      |
               |  |  +--evidence_required--> evidence-retriever ------------+
               |  |                                                         |
               |  +--evidence_ready--> answer (propose + critic screen) --> END
               |  +--worker_failed / nothing_to_brief --------------------> END
               +------------------------------------------------------------+

- ``intake-extractor`` reads the document into a strict schema. One worker for
  every document type, dispatched by ``doc_type``, so the routing log records
  which reading of "intake" ran (W2_ARCHITECTURE section 2).
- ``evidence-retriever`` runs sparse + dense retrieval, RRF, then the reranker.
- ``answer`` proposes considerations from the top evidence only, and the
  critic - ``build_briefing``'s admissibility screen - drops any uncited,
  directive or unsupported claim before display. The critic is a validation
  step, not a third worker (CR4 calls a critic agent extension work).

Every routing decision is logged (source, target, reason code, doc_type), is a
``supervisor`` span in the trace, and is returned to the panel.

LangSmith stays off. LangGraph is an in-process MIT library; LangSmith is a
hosted platform it can report to when ``LANGSMITH_TRACING`` is set. That would
send graph state - the document - to a third party, so the graph refuses to
build when it is set. Tracing goes only through the masked Langfuse spans below.
No checkpointer is configured: a briefing is one request, and a checkpoint of
this state would be PHI at rest.
"""

from __future__ import annotations

import operator
import os
from functools import lru_cache
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.briefing import ConsiderationCandidate, build_briefing, render_briefing
from app.document_briefing import (
    ANSWER_MAX_OUTPUT_TOKENS,
    ANSWER_SYSTEM_PROMPT,
    QUERY_TOP_K,
    BriefingStatus,
    ConsiderationDraftSet,
    DocumentBriefingRequest,
    DocumentBriefingResponse,
    Provenance,
    RoutingDecision,
    _answer_content,
    build_query,
    chart_facts,
    combine_documents,
    drafts_to_candidates,
    get_retriever,
    read_lab_document,
)
from app.documents import LabDocument
from app.evidence import EvidencePackage, RetrievalQuery
from app.observability import generation, log_event, score, span
from app.providers.base import ModelProvider, ProviderError
from app.reranker import BedrockReranker, FakeReranker

INTAKE_EXTRACTOR = "intake-extractor"
EVIDENCE_RETRIEVER = "evidence-retriever"
ANSWER = "answer"
FINISH = "finish"

Target = Literal["intake-extractor", "evidence-retriever", "answer", "finish"]

# The environment switches that would make LangChain report to LangSmith.
LANGSMITH_SWITCHES = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")


class LangSmithEnabledError(RuntimeError):
    """LangSmith tracing is switched on; graph state would leave the process."""


def assert_langsmith_off() -> None:
    for switch in LANGSMITH_SWITCHES:
        if os.environ.get(switch, "").strip().lower() in {"1", "true", "yes", "on"}:
            raise LangSmithEnabledError(f"{switch} is set; unset it - traces go to Langfuse only")


class BriefingState(TypedDict, total=False):
    """Graph state. Never checkpointed, never traced as a whole."""

    request: DocumentBriefingRequest
    doc_type: str
    document: LabDocument | None
    evidence: EvidencePackage | None
    retrieval_candidates: int
    candidates: list[ConsiderationCandidate]
    briefed: bool
    answer_model: str
    status: BriefingStatus
    reason: str | None
    next: Target
    routing: Annotated[list[RoutingDecision], operator.add]


def _deps(config: RunnableConfig) -> tuple[ModelProvider, FakeReranker | BedrockReranker]:
    c = config["configurable"]
    return c["provider"], c["reranker"]


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #


def decide(state: BriefingState) -> tuple[Target, str]:
    """The routing rule, as a pure function so every branch is unit-tested."""
    if state.get("status") is BriefingStatus.DEGRADED:
        return FINISH, "worker_failed"
    if state.get("document") is None:
        return INTAKE_EXTRACTOR, "document_pending_extraction"
    if state.get("evidence") is None:
        return EVIDENCE_RETRIEVER, "evidence_required"
    if not state.get("briefed"):
        return ANSWER, "evidence_ready"
    return FINISH, "briefing_complete"


async def supervisor(state: BriefingState) -> dict[str, Any]:
    target, reason = decide(state)
    step = len(state.get("routing", [])) + 1
    decision = RoutingDecision(step=step, source="supervisor", target=target, reason_code=reason, doc_type=state["doc_type"])
    with span("supervisor", stage=target, reason_code=reason):
        log_event(
            "routing.decision",
            cid=state["request"].correlation_id,
            step=step,
            target=target,
            reason_code=reason,
            doc_type=state["doc_type"],
        )
    return {"next": target, "routing": [decision]}


# --------------------------------------------------------------------------- #
# Workers
# --------------------------------------------------------------------------- #


async def intake_extractor(state: BriefingState, config: RunnableConfig) -> dict[str, Any]:
    provider, _ = _deps(config)
    request = state["request"]
    with span(INTAKE_EXTRACTOR, stage=state["doc_type"]) as attrs:
        document, reason = await read_lab_document(
            document_id=request.document_id,
            document_base64=request.document_base64,
            media_type=request.media_type,
            provider=provider,
        )
        if document is None:
            attrs["outcome"] = "degraded"
            return {"status": BriefingStatus.DEGRADED, "reason": reason}
        attrs["outcome"] = "ok"
        attrs["records"] = len(document.results)
        return {"document": document}


async def evidence_retriever(state: BriefingState, config: RunnableConfig) -> dict[str, Any]:
    _, reranker = _deps(config)
    document = state["document"]
    assert document is not None  # the supervisor only routes here after extraction
    with span(EVIDENCE_RETRIEVER) as attrs:
        retriever = get_retriever()
        query = build_query(document)
        retrieved = retriever.retrieve(RetrievalQuery(text=query, corpus_version=retriever.corpus_version))
        evidence = reranker.rerank(query, retrieved.candidates, top_k=QUERY_TOP_K)
        attrs["outcome"] = evidence.status.value
        attrs["records"] = len(evidence.snippets)
        return {"evidence": evidence, "retrieval_candidates": len(retrieved.candidates)}


async def answer(state: BriefingState, config: RunnableConfig) -> dict[str, Any]:
    provider, _ = _deps(config)
    document, evidence = state["document"], state["evidence"]
    assert document is not None and evidence is not None
    update: dict[str, Any] = {"briefed": True, "candidates": [], "answer_model": "none"}
    if not evidence.snippets:
        return update  # nothing grounded to propose from; the critic still screens the record lines
    with span(ANSWER) as attrs:
        try:
            # Traced as a generation so its latency, tokens and cost are visible;
            # input/output are never set - they hold patient values.
            with generation("answer_considerations") as gen:
                parsed = await provider.parse_structured(
                    system=ANSWER_SYSTEM_PROMPT,
                    content=_answer_content(document, evidence),
                    schema=ConsiderationDraftSet,
                    max_tokens=ANSWER_MAX_OUTPUT_TOKENS,
                    effort="low",
                )
                gen["usage"] = parsed.usage
        except ProviderError:
            # The record-derived headings still render; only the guidance is missing.
            attrs["outcome"] = "degraded"
            update.update(status=BriefingStatus.DEGRADED, reason="answer_model_unavailable")
            return update
        attrs["outcome"] = "ok"
        update.update(candidates=drafts_to_candidates(parsed.output, document), answer_model=parsed.usage.model)
        return update


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #


def _route(state: BriefingState) -> str:
    return state["next"]


def _after_answer(state: BriefingState) -> str:
    # A failed answer model is not re-routed: the briefing renders without guidance.
    return END


@lru_cache(maxsize=1)
def build_graph() -> Any:
    assert_langsmith_off()
    g = StateGraph(BriefingState)
    g.add_node("supervisor", supervisor)
    g.add_node(INTAKE_EXTRACTOR, intake_extractor)
    g.add_node(EVIDENCE_RETRIEVER, evidence_retriever)
    g.add_node(ANSWER, answer)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges(
        "supervisor",
        _route,
        {INTAKE_EXTRACTOR: INTAKE_EXTRACTOR, EVIDENCE_RETRIEVER: EVIDENCE_RETRIEVER, ANSWER: ANSWER, FINISH: END},
    )
    g.add_edge(INTAKE_EXTRACTOR, "supervisor")
    g.add_edge(EVIDENCE_RETRIEVER, "supervisor")
    g.add_edge(ANSWER, "supervisor")
    return g.compile()


async def run_supervised_briefing(
    request: DocumentBriefingRequest,
    *,
    provider: ModelProvider,
    reranker: FakeReranker | BedrockReranker,
) -> DocumentBriefingResponse:
    """Run the graph and assemble the panel's response from its final state."""
    assert_langsmith_off()  # checked per request too: the environment can change after build
    initial: BriefingState = {"request": request, "doc_type": "lab_pdf", "routing": [], "status": BriefingStatus.OK, "reason": None}
    if request.documents is not None:
        # Stored extractions (ADR-012): the documents are already read, so the
        # supervisor's first decision is evidence retrieval, never extraction.
        initial["document"] = combine_documents([d.extraction for d in request.documents])
    final: BriefingState = await build_graph().ainvoke(
        initial,
        config={"configurable": {"provider": provider, "reranker": reranker}, "recursion_limit": 12},
    )
    base = {
        "correlation_id": request.correlation_id,
        "patient_uuid": request.patient_uuid,
        "document_id": request.document_ids[0],
        "document_ids": request.document_ids,
        "routing": tuple(final.get("routing", [])),
    }
    document, evidence = final.get("document"), final.get("evidence")
    if document is None or evidence is None:
        return DocumentBriefingResponse(**base, status=BriefingStatus.DEGRADED, degraded_reason=final.get("reason"))

    candidates = final.get("candidates", [])
    briefing = build_briefing(
        document=document,
        evidence=evidence,
        prior_facts=chart_facts(request.prior_facts),
        considerations=candidates,
        question=request.question,
    )

    # CR7 per-encounter signals, as scores on the encounter trace. Counts, rates
    # and fixed labels only - never a value, a quote or an identifier.
    meta = document.extraction_metadata
    score("extraction_results", len(document.results))
    score("extraction_verified_fraction", meta.verified_fraction)
    score("extraction_unreadable", meta.unreadable_count)
    score("retrieval_candidates", final.get("retrieval_candidates", 0))
    score("evidence_snippets", len(evidence.snippets))
    score("evidence_status", evidence.status.value, data_type="CATEGORICAL")
    score("considerations_shown", len(briefing.what_to_consider))
    score("claims_withheld", len(briefing.dropped))
    score("routing_steps", len(base["routing"]))
    # The encounter's eval outcome: every displayed claim survived screening.
    score("briefing_grounded", not briefing.dropped, data_type="BOOLEAN")

    log_event(
        "document_briefing.completed",
        cid=request.correlation_id,
        documents=len(request.document_ids),
        prior_facts=len(request.prior_facts),
        results=len(document.results),
        snippets=len(evidence.snippets),
        considerations_proposed=len(candidates),
        considerations_shown=len(briefing.what_to_consider),
        dropped=len(briefing.dropped),
        evidence_status=evidence.status.value,
        route=[d.target for d in base["routing"]],
    )

    retriever = get_retriever()
    return DocumentBriefingResponse(
        **base,
        status=final.get("status", BriefingStatus.OK),
        degraded_reason=final.get("reason"),
        briefing=briefing,
        rendered_text=render_briefing(briefing),
        provenance=Provenance(
            extraction_model=meta.model_id,
            answer_model=final.get("answer_model", "none"),
            reranker=reranker.model_id,
            corpus_version=retriever.corpus_version,
            evidence_status=evidence.status,
        ),
    )


__all__ = [
    "ANSWER",
    "EVIDENCE_RETRIEVER",
    "INTAKE_EXTRACTOR",
    "LangSmithEnabledError",
    "assert_langsmith_off",
    "build_graph",
    "decide",
    "run_supervised_briefing",
]
