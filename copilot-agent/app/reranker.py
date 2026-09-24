"""Reranking and grounded evidence packaging (feature PRD F09, ADR-002).

Thirty fused candidates are not evidence; they are noise. This module scores
them against the question, keeps the best five, and packages each one with the
metadata a citation needs - then hands the answer model an
:class:`~app.evidence.EvidencePackage` and nothing else.

**The boundary is the point.** ADR-002 s10.1: no Bedrock or ``boto3`` type
appears outside this module. ``boto3`` is imported *lazily, inside a method*,
and is deliberately **not** a declared dependency, so the offline CI gate
installs and runs without an AWS SDK at all. ``tests/test_reranker.py`` asserts
both facts by grepping the package - an import elsewhere is a review failure
(F09 s16, **D**).

**The fake is not a stub.** :class:`FakeReranker` satisfies every non-live test
:class:`BedrockReranker` satisfies (F09 s16, **L**). If it did not, the key-free
gate would prove nothing: it would be exercising a different object than
production runs. It scores by lexical coverage of the query - crude, but
deterministic, offline and, unlike an echo of the fused order, capable of
actually reordering.

**Failure is explicit, and it is a state, not an exception.** ADR-002 s10.6: on
any rerank failure the package is ``unavailable``, with an empty snippet list
and a fixed reason, and the answer must then say guideline evidence is
unavailable. It must never quietly return fewer results as though that were
success, and it must never pass unreranked candidates through - that would
silently drop the stage CR3 requires while still looking like it worked. Every
failure path in this module ends at :meth:`_unavailable`.

**On the "no candidates" case.** F09 s10 says an empty candidate list should be
``ok`` with no snippets. The frozen contract in ``app.evidence`` forbids that
combination outright - ``ok`` requires at least one snippet - so this module
reports ``degraded`` with :data:`NO_CANDIDATES_REASON`. That divergence from the
PRD's table is deliberate and belongs to the contract, not to this file.

PHI: the query is minimised clinical concepts and is **never logged**. Chunk
ids, scores, counts, model id and latency are logged (F09 s11).

Deferred from F09 s7.4/s7.5, and honestly not built here: the token-bucket rate
limiter, the concurrency cap and the circuit breaker. They matter only against
live Bedrock, which nothing in this sprint calls; bounded retry and a hard
timeout are implemented because the failure path depends on them.
"""

from __future__ import annotations

import random
import time
from collections.abc import Sequence
from typing import Any

from app.corpus import Corpus
from app.evidence import (
    Candidate,
    EvidencePackage,
    EvidenceSnippet,
    GuidelineCitation,
    RetrievalStatus,
)
from app.observability import span
from app.providers.base import ProviderConfigurationError
from app.retrieval import tokenize

#: F09 s7.6 / s18 - "only the top grounded evidence" reaches the answer model.
DEFAULT_TOP_K = 5

#: ADR-002. Pinned, logged per call, and never inferred from configuration.
BEDROCK_RERANK_MODEL_ID = "cohere.rerank-v3-5:0"
DEFAULT_BEDROCK_REGION = "us-west-2"

#: Honest about being a local heuristic; no reader can mistake it for a model.
FAKE_RERANKER_MODEL_ID = "fake-lexical-coverage-v1 (local, deterministic)"

#: Fixed reason strings. Displayed to a physician, so never a raw exception and
#: never patient-specific (``app.evidence.EvidencePackage.degraded_reason``).
RERANKER_UNAVAILABLE_REASON = "guideline_reranker_unavailable"
NO_CANDIDATES_REASON = "no_guideline_candidates_retrieved"

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_ATTEMPTS = 2
RETRY_BASE_SECONDS = 0.05


def _sleep(seconds: float) -> None:  # patched out in tests that exercise the retry budget
    time.sleep(seconds)


# --------------------------------------------------------------------------- #
# Packaging - shared by every implementation
# --------------------------------------------------------------------------- #


def _snippet(corpus: Corpus, candidate: Candidate, score: float) -> EvidenceSnippet:
    """One candidate as citable evidence. Raises ``KeyError`` if the chunk no longer resolves."""
    citation = corpus.citation_for(candidate.chunk_id)
    return EvidenceSnippet(
        chunk_id=candidate.chunk_id,
        text=citation.quote,
        doc_title=citation.document_title,
        publisher=citation.publisher,
        section=candidate.section,
        relevance_score=score,
        citation=GuidelineCitation.from_corpus(citation),
    )


class _PackagingMixin:
    """The three outcomes every reranker can have, built once."""

    corpus: Corpus
    model_id: str

    def _package(self, scored: Sequence[tuple[Candidate, float]]) -> EvidencePackage:
        snippets = tuple(_snippet(self.corpus, candidate, score) for candidate, score in scored)
        if not snippets:
            return self._degraded(NO_CANDIDATES_REASON)
        return EvidencePackage(
            snippets=snippets,
            corpus_version=self.corpus.corpus_version,
            reranker_model_id=self.model_id,
            status=RetrievalStatus.OK,
        )

    def _degraded(self, reason: str) -> EvidencePackage:
        """Impaired or empty, but the reranker itself answered."""
        return EvidencePackage(
            snippets=(),
            corpus_version=self.corpus.corpus_version,
            reranker_model_id=self.model_id,
            status=RetrievalStatus.DEGRADED,
            degraded_reason=reason,
        )

    def _unavailable(self) -> EvidencePackage:
        """The reranker could not answer. No candidates pass through (ADR-002 s10.6)."""
        return EvidencePackage(
            snippets=(),
            corpus_version=self.corpus.corpus_version,
            reranker_model_id=self.model_id,
            status=RetrievalStatus.UNAVAILABLE,
            degraded_reason=RERANKER_UNAVAILABLE_REASON,
        )


def _record(attrs: dict[str, Any], package: EvidencePackage, *, candidate_count: int) -> EvidencePackage:
    """Fill the span. Chunk ids and scores only - never the query (F09 s11, s12)."""
    attrs["candidate_count"] = candidate_count
    attrs["results_count"] = len(package.snippets)
    attrs["selected_chunk_ids"] = [snippet.chunk_id for snippet in package.snippets]
    attrs["relevance_scores"] = [round(snippet.relevance_score, 6) for snippet in package.snippets]
    attrs["stage"] = str(package.status)
    attrs["outcome"] = "ok" if package.status is RetrievalStatus.OK else "error"
    if package.degraded_reason:
        attrs["reason_code"] = package.degraded_reason
    return package


# --------------------------------------------------------------------------- #
# The fake: what CI actually runs
# --------------------------------------------------------------------------- #


class FakeReranker(_PackagingMixin):
    """Deterministic, offline, no network. The reranker every CI run exercises.

    Relevance is the fraction of the query's distinct terms the passage
    contains. That is not semantic scoring and does not pretend to be - but it
    is a genuine reordering signal, so a test asserting that reranking promotes
    a chunk fusion buried at rank 8 is a real assertion rather than a tautology.

    Ties break on fused rank, then chunk id, so identical input gives identical
    output down to the byte (F09 s13 AC-4).

    ``available=False`` is the demo's kill switch (F09 s15): it drives the same
    ``unavailable`` path a Bedrock outage would, without an outage.
    """

    name = "fake-reranker"
    model_id = FAKE_RERANKER_MODEL_ID

    def __init__(self, corpus: Corpus, *, available: bool = True) -> None:
        self.corpus = corpus
        self._available = available

    def rerank(
        self, query: str, candidates: tuple[Candidate, ...], *, top_k: int = DEFAULT_TOP_K
    ) -> EvidencePackage:
        with span("rerank", model=self.model_id) as attrs:
            if not self._available:
                package = self._unavailable()
            elif not candidates:
                package = self._degraded(NO_CANDIDATES_REASON)
            else:
                package = self._package(self._score(query, candidates)[:top_k])
            return _record(attrs, package, candidate_count=len(candidates))

    def _score(self, query: str, candidates: Sequence[Candidate]) -> list[tuple[Candidate, float]]:
        terms = set(tokenize(query))
        scored: list[tuple[Candidate, float]] = []
        for candidate in candidates:
            present = terms & set(tokenize(candidate.text))
            scored.append((candidate, len(present) / len(terms) if terms else 0.0))
        scored.sort(key=lambda pair: (-pair[1], pair[0].fused_rank, pair[0].chunk_id))
        return scored


# --------------------------------------------------------------------------- #
# The Bedrock adapter: the only place boto3 may appear
# --------------------------------------------------------------------------- #


class BedrockReranker(_PackagingMixin):
    """Cohere Rerank 3.5 through the Bedrock ``Rerank`` API (ADR-002).

    A skeleton in the literal sense: the request and response shapes, the model
    pinning, the bounded retry and the failure semantics are real; nothing in
    this repository calls it against live Bedrock, and the gate never can,
    because ``boto3`` is not installed.

    Pass ``client`` to drive the adapter without an SDK - that is how the
    protocol-conformance tests run it through the same assertions as the fake.
    """

    name = "bedrock-cohere-rerank"

    def __init__(
        self,
        corpus: Corpus,
        *,
        client: Any | None = None,
        region: str = DEFAULT_BEDROCK_REGION,
        model_id: str = BEDROCK_RERANK_MODEL_ID,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.corpus = corpus
        self.model_id = model_id
        self.region = region
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self._client = client

    # -- the SDK boundary ---------------------------------------------------- #

    def client(self) -> Any:
        """The ``bedrock-agent-runtime`` client.

        ``boto3`` is imported here and nowhere else, and only when a call is
        actually made, so importing this module costs nothing and needs nothing.
        """
        if self._client is not None:
            return self._client
        try:
            import boto3  # noqa: PLC0415 - lazy by design (ADR-002 s10.1)
            from botocore.config import Config  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatched __import__
            raise ProviderConfigurationError(
                "boto3 is not installed, so the Bedrock reranker cannot be used. "
                "It is deliberately not a declared dependency: the offline gate runs on FakeReranker."
            ) from exc

        self._client = boto3.client(
            "bedrock-agent-runtime",
            region_name=self.region,
            config=Config(
                read_timeout=self.timeout_seconds,
                connect_timeout=self.timeout_seconds,
                retries={"max_attempts": 0},  # retry is bounded here, not by botocore
            ),
        )
        return self._client

    # -- the protocol -------------------------------------------------------- #

    def rerank(
        self, query: str, candidates: tuple[Candidate, ...], *, top_k: int = DEFAULT_TOP_K
    ) -> EvidencePackage:
        with span("rerank", model=self.model_id) as attrs:
            if not candidates:
                package = self._degraded(NO_CANDIDATES_REASON)
            else:
                package = self._call(query, candidates, top_k=top_k)
            return _record(attrs, package, candidate_count=len(candidates))

    def _call(self, query: str, candidates: tuple[Candidate, ...], *, top_k: int) -> EvidencePackage:
        request = self._request(query, candidates, top_k=top_k)
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client().rerank(**request)
                return self._package(self._parse(response, candidates))
            except Exception:  # noqa: BLE001 - every fault ends in one explicit state
                if attempt >= self.max_attempts:
                    return self._unavailable()
                # Jittered backoff: a synchronised retry storm is how a
                # recovering service is knocked over a second time.
                _sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)) * (0.5 + random.random()))  # noqa: S311
        return self._unavailable()  # pragma: no cover - the loop always returns

    def _request(self, query: str, candidates: Sequence[Candidate], *, top_k: int) -> dict[str, Any]:
        return {
            "queries": [{"type": "TEXT", "textQuery": {"text": query}}],
            "sources": [
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": candidate.text}},
                }
                for candidate in candidates
            ],
            "rerankingConfiguration": {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "numberOfResults": min(top_k, len(candidates)),
                    "modelConfiguration": {
                        "modelArn": f"arn:aws:bedrock:{self.region}::foundation-model/{self.model_id}"
                    },
                },
            },
        }

    def _parse(
        self, response: Any, candidates: tuple[Candidate, ...]
    ) -> list[tuple[Candidate, float]]:
        """Validate the response against the candidates we sent.

        Anything unexpected raises, and the caller turns that into
        ``unavailable`` - a malformed response is not partial success
        (F09 s10, "Invalid schema output").
        """
        results = response["results"]
        if not isinstance(results, list):
            raise ValueError("Bedrock rerank response 'results' is not a list")

        scored: list[tuple[Candidate, float]] = []
        seen: set[int] = set()
        for entry in results:
            index = int(entry["index"])
            if not 0 <= index < len(candidates):
                raise ValueError(f"Bedrock rerank returned index {index} outside the candidate list")
            if index in seen:
                raise ValueError(f"Bedrock rerank returned index {index} twice")
            seen.add(index)
            scored.append((candidates[index], float(entry["relevanceScore"])))
        return scored


__all__ = [
    "BEDROCK_RERANK_MODEL_ID",
    "DEFAULT_BEDROCK_REGION",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TOP_K",
    "FAKE_RERANKER_MODEL_ID",
    "NO_CANDIDATES_REASON",
    "RERANKER_UNAVAILABLE_REASON",
    "BedrockReranker",
    "FakeReranker",
]
