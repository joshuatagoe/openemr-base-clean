"""Sparse + dense hybrid retrieval over the guideline corpus (feature PRD F08).

Two retrievers, one fused candidate list, no network and no model download.

**Sparse** is BM25 over the corpus chunk text, implemented here rather than
pulled in as a dependency: the scoring is forty lines, and a retriever whose
behaviour is visible in the repository is easier to defend than one whose
tokeniser is a transitive import (F08 s18 chose "a BM25 library" for being
deterministic, offline and CI-friendly; a hand-rolled BM25 is all three and
adds no supply chain).

**Dense** is a hashed character-n-gram embedding with cosine similarity. It is
deliberately *not* a learned model. F08 s18 recommended Bedrock embeddings; that
would put a second PHI egress point in the retrieval path and make the offline
CI gate depend on an AWS key, which F09 s17 forbids outright for the reranker
and which applies with equal force here. The honest description of what this is
- ``hashed-ngram-v1 (local, deterministic)`` - is recorded on every result, so
no reader can mistake it for a semantic embedding model. It buys what character
n-grams buy: robustness to morphology and spelling variation ("individualized"
against "Individualize"), not conceptual paraphrase. Swapping in a real
embedding source is a matter of passing a different object as ``dense``.

**Fusion** is Reciprocal Rank Fusion with ``k = 60`` (F08 s8), which needs no
score calibration between two incomparable scoring systems. Up to 30 candidates
survive, from a top 20 per retriever.

Three rules shape the rest.

**A version mismatch is a hard error.** A query naming a corpus version the
index was not built from raises :class:`CorpusVersionMismatchError` rather than
answering from a stale index, because a silently stale retrieval is
indistinguishable from a correct one at the point of use (F08 s7.6).

**An empty result is an outcome, not a failure.** A query with no relevant
passage returns no candidates. The dense retriever will always produce *some*
nearest neighbour, so a relevance floor keeps noise from being presented as
evidence (F08 s14, adversarial).

**Degradation is explicit.** If the dense retriever fails, sparse-only
candidates are returned and the reason is recorded on the retriever and in the
span - never an exception reaching the caller, never a silent narrowing
(F08 s10).

PHI: the query text is minimised clinical concepts and is **never logged**.
Chunk ids, ranks, scores and counts are public-corpus metadata and are logged
freely (F08 s11).
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.corpus import Corpus, CorpusChunk, build_corpus
from app.evidence import Candidate, RetrievalQuery, RetrievalResult
from app.observability import span

# --------------------------------------------------------------------------- #
# Pinned parameters (F08 s8, s18)
# --------------------------------------------------------------------------- #

#: Honest by construction: this is a local hashing scheme, not a learned model.
EMBEDDING_MODEL_ID = "hashed-ngram-v1 (local, deterministic)"

#: RRF constant. F08 s8, the standard value.
RRF_K = 60

#: F08 s7.4 / s18 - 20 + 20 fused down to at most 30.
MAX_CANDIDATES = 30
DEFAULT_TOP_K_PER_RETRIEVER = 20

#: BM25 free parameters, the conventional defaults.
BM25_K1 = 1.5
BM25_B = 0.75

#: Character-n-gram width and embedding width for the dense index. The width is
#: generous because hash collisions are what destroy this scheme: at 512 buckets
#: every chunk occupies nearly every dimension, invented words land on top of
#: real ones, and the cosine stops discriminating. Vectors are stored sparsely,
#: so a wide space costs nothing but a larger modulus.
NGRAM_SIZE = 4
EMBEDDING_DIMENSIONS = 16384

#: Below this cosine similarity a dense hit is nearest-neighbour noise rather
#: than evidence. F08 s14 requires that a query with no relevant chunk return
#: empty rather than the least-bad chunk dressed up as relevant.
DENSE_MIN_SIMILARITY = 0.10

#: The fixed reason recorded when the dense retriever fails (F08 s10).
DENSE_UNAVAILABLE_REASON = "dense_retriever_unavailable"

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")

#: Tokens that carry no retrieval signal. Small and closed on purpose: a large
#: stoplist starts deciding clinical meaning, which is not its job.
STOPWORDS = frozenset(
    {
        "a", "about", "an", "and", "any", "are", "as", "at", "be", "been", "but", "by", "can", "do", "does",
        "for", "from", "had", "has", "have", "how", "i", "if", "in", "into", "is", "it", "its", "may", "might",
        "no", "not", "of", "on", "or", "should", "so", "such", "than", "that", "the", "their", "them", "then",
        "there", "these", "they", "this", "to", "was", "were", "what", "when", "which", "who", "why", "will",
        "with", "would", "you", "your",
    }
)


class RetrievalError(RuntimeError):
    """Base class for retrieval faults that must not be swallowed."""


class CorpusVersionMismatchError(RetrievalError):
    """The query names a corpus version this index was not built from (F08 s7.6)."""


# --------------------------------------------------------------------------- #
# Tokenisation
# --------------------------------------------------------------------------- #


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords removed.

    ``a1c`` and ``7.0`` survive intact: a tokeniser that splits a test name or a
    threshold apart loses exactly the terms clinical queries are precise about.
    """
    return [token for token in _TOKEN.findall(text.lower()) if token not in STOPWORDS]


# --------------------------------------------------------------------------- #
# Sparse: BM25
# --------------------------------------------------------------------------- #


class SparseIndex:
    """BM25 over chunk text. Deterministic, in-memory, built from the corpus."""

    model_id = "bm25-k1-1.5-b-0.75"

    def __init__(self, chunks: Sequence[CorpusChunk]) -> None:
        self._chunk_ids: tuple[str, ...] = tuple(chunk.chunk_id for chunk in chunks)
        self._term_frequencies: dict[str, Counter[str]] = {}
        self._lengths: dict[str, int] = {}
        document_frequency: Counter[str] = Counter()

        for chunk in chunks:
            tokens = tokenize(chunk.text)
            counts = Counter(tokens)
            self._term_frequencies[chunk.chunk_id] = counts
            self._lengths[chunk.chunk_id] = len(tokens)
            document_frequency.update(counts.keys())

        self._document_count = len(self._chunk_ids)
        self._average_length = (
            sum(self._lengths.values()) / self._document_count if self._document_count else 0.0
        )
        self._idf = {
            term: math.log(1.0 + (self._document_count - df + 0.5) / (df + 0.5))
            for term, df in document_frequency.items()
        }

        # chunk_id -> the chunks containing each term, so scoring touches only
        # the postings a query actually names.
        self._postings: dict[str, list[str]] = {}
        for chunk_id, counts in self._term_frequencies.items():
            for term in counts:
                self._postings.setdefault(term, []).append(chunk_id)

    def search(self, text: str, *, top_k: int = DEFAULT_TOP_K_PER_RETRIEVER) -> list[tuple[str, float]]:
        """The ``top_k`` highest-scoring chunks, descending. Zero-scoring chunks are omitted."""
        query_terms = [term for term in set(tokenize(text)) if term in self._postings]
        if not query_terms:
            return []

        scores: dict[str, float] = {}
        for term in query_terms:
            idf = self._idf[term]
            for chunk_id in self._postings[term]:
                tf = self._term_frequencies[chunk_id][term]
                length = self._lengths[chunk_id]
                denominator = tf + BM25_K1 * (1.0 - BM25_B + BM25_B * length / (self._average_length or 1.0))
                scores[chunk_id] = scores.get(chunk_id, 0.0) + idf * (tf * (BM25_K1 + 1.0)) / denominator

        return _rank(scores, top_k=top_k, floor=0.0)


# --------------------------------------------------------------------------- #
# Dense: hashed character n-grams
# --------------------------------------------------------------------------- #


def _ngram_bucket(ngram: str) -> int:
    """A stable bucket for an n-gram.

    ``blake2b`` rather than ``hash()``: Python's string hash is salted per
    process, so an index built with it would rank differently on every run -
    the exact non-determinism F08 s13 AC-7 forbids.
    """
    digest = hashlib.blake2b(ngram.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % EMBEDDING_DIMENSIONS


def ngram_weights(text: str, *, ngram_size: int = NGRAM_SIZE) -> dict[int, float]:
    """Sublinear term frequencies over hashed character n-grams, unweighted and unnormalised."""
    tokens = tokenize(text)
    if not tokens:
        return {}

    padded = f" {' '.join(tokens)} "
    if len(padded) < ngram_size:
        padded = padded.ljust(ngram_size)

    counts: Counter[int] = Counter(
        _ngram_bucket(padded[i : i + ngram_size]) for i in range(len(padded) - ngram_size + 1)
    )
    # A word repeated ten times is not ten times the evidence.
    return {bucket: 1.0 + math.log(count) for bucket, count in counts.items()}


def _normalise(vector: Mapping[int, float]) -> dict[int, float]:
    norm = math.sqrt(sum(weight * weight for weight in vector.values()))
    if norm == 0.0:
        return {}
    return {bucket: weight / norm for bucket, weight in vector.items()}


def cosine(left: Mapping[int, float], right: Mapping[int, float]) -> float:
    """Cosine similarity of two already-normalised sparse vectors."""
    if len(left) > len(right):
        left, right = right, left
    return sum(weight * right.get(bucket, 0.0) for bucket, weight in left.items())


class DenseIndex:
    """Cosine similarity over local, IDF-weighted hashed-n-gram vectors. No model, no network.

    The IDF weighting is what makes the similarity *mean* something. Raw
    character n-grams are promiscuous - every English passage shares " th",
    "ing " and "tion" - so an unweighted cosine puts invented words within a
    few hundredths of a real clinical term, and no relevance floor can then
    separate evidence from noise. Weighting each bucket by how rare it is in
    the corpus, and giving a bucket the corpus has never seen the maximum
    weight, pushes an out-of-vocabulary query down where it belongs.
    """

    model_id = EMBEDDING_MODEL_ID

    def __init__(self, chunks: Sequence[CorpusChunk], *, minimum_similarity: float = DENSE_MIN_SIMILARITY) -> None:
        raw = {chunk.chunk_id: ngram_weights(chunk.text) for chunk in chunks}

        document_frequency: Counter[int] = Counter()
        for vector in raw.values():
            document_frequency.update(vector.keys())

        document_count = len(raw)
        self._idf = {
            bucket: math.log(1.0 + (document_count - df + 0.5) / (df + 0.5))
            for bucket, df in document_frequency.items()
        }
        # A bucket the corpus has never produced is maximally distinctive - and
        # matches nothing, so it only ever costs the query similarity.
        self._unseen_idf = math.log(1.0 + (document_count + 0.5) / 0.5) if document_count else 1.0

        self._vectors = {
            chunk_id: _normalise({bucket: weight * self._idf[bucket] for bucket, weight in vector.items()})
            for chunk_id, vector in raw.items()
        }
        self._minimum_similarity = minimum_similarity

    def embed(self, text: str) -> dict[int, float]:
        """The query vector, in the same weighted space as the indexed chunks."""
        raw = ngram_weights(text)
        if not raw:
            return {}
        return _normalise(
            {bucket: weight * self._idf.get(bucket, self._unseen_idf) for bucket, weight in raw.items()}
        )

    def search(self, text: str, *, top_k: int = DEFAULT_TOP_K_PER_RETRIEVER) -> list[tuple[str, float]]:
        query_vector = self.embed(text)
        if not query_vector:
            return []
        scores = {chunk_id: cosine(query_vector, vector) for chunk_id, vector in self._vectors.items()}
        return _rank(scores, top_k=top_k, floor=self._minimum_similarity)


def _rank(scores: Mapping[str, float], *, top_k: int, floor: float) -> list[tuple[str, float]]:
    """Descending by score, ties broken by chunk id so the order never depends on dict insertion."""
    kept = [(chunk_id, score) for chunk_id, score in scores.items() if score > floor]
    kept.sort(key=lambda pair: (-pair[1], pair[0]))
    return kept[:top_k]


class Retriever(Protocol):
    """What :class:`HybridRetriever` needs of either half. Swap either for a real service."""

    model_id: str

    def search(self, text: str, *, top_k: int) -> list[tuple[str, float]]:
        ...


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FusedEntry:
    """One chunk's standing after fusion, before corpus metadata is attached."""

    chunk_id: str
    sparse_rank: int | None
    dense_rank: int | None
    fused_score: float
    fused_rank: int


def reciprocal_rank_fusion(
    sparse_ids: Iterable[str],
    dense_ids: Iterable[str],
    *,
    k: int = RRF_K,
    limit: int = MAX_CANDIDATES,
) -> tuple[FusedEntry, ...]:
    """``score(chunk) = sum over retrievers of 1 / (k + rank)`` (F08 s8).

    Ranks are 1-based. A chunk found by one retriever still scores; a chunk
    found by both scores twice and therefore outranks a single-retriever chunk
    at the same rank. Equal scores break on chunk id, so the output order is a
    function of the inputs alone.
    """
    sparse_rank = {chunk_id: index for index, chunk_id in enumerate(sparse_ids, start=1)}
    dense_rank = {chunk_id: index for index, chunk_id in enumerate(dense_ids, start=1)}

    scored: list[tuple[float, str]] = []
    for chunk_id in set(sparse_rank) | set(dense_rank):
        score = 0.0
        if chunk_id in sparse_rank:
            score += 1.0 / (k + sparse_rank[chunk_id])
        if chunk_id in dense_rank:
            score += 1.0 / (k + dense_rank[chunk_id])
        scored.append((score, chunk_id))

    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return tuple(
        FusedEntry(
            chunk_id=chunk_id,
            sparse_rank=sparse_rank.get(chunk_id),
            dense_rank=dense_rank.get(chunk_id),
            fused_score=score,
            fused_rank=position,
        )
        for position, (score, chunk_id) in enumerate(scored[:limit], start=1)
    )


# --------------------------------------------------------------------------- #
# The hybrid retriever
# --------------------------------------------------------------------------- #


class HybridRetriever:
    """Sparse and dense over one corpus, fused into a single ranked candidate list."""

    def __init__(
        self,
        corpus: Corpus,
        *,
        sparse: Retriever | None = None,
        dense: Retriever | None = None,
        embedding_model_id: str | None = None,
    ) -> None:
        self.corpus = corpus
        self.sparse = sparse if sparse is not None else SparseIndex(corpus.chunks)
        self.dense = dense if dense is not None else DenseIndex(corpus.chunks)
        self.embedding_model_id = embedding_model_id or getattr(self.dense, "model_id", EMBEDDING_MODEL_ID)
        #: The reason the most recent retrieval was degraded, or ``None``. Read
        #: by the caller so a degraded retrieval can qualify the answer (F08 s10).
        self.last_degraded_reason: str | None = None

    @property
    def corpus_version(self) -> str:
        return self.corpus.corpus_version

    def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        """Retrieve, fuse and package. Raises only on a corpus version mismatch."""
        if query.corpus_version != self.corpus.corpus_version:
            raise CorpusVersionMismatchError(
                f"index is built for corpus {self.corpus.corpus_version!r}, "
                f"query declares {query.corpus_version!r}"
            )

        started = time.perf_counter()
        self.last_degraded_reason = None
        top_k = query.top_k_per_retriever

        with span(
            "retrieval.hybrid",
            corpus_version=self.corpus.corpus_version,
            embedding_model_id=self.embedding_model_id,
        ) as attrs:
            sparse_hits = self.sparse.search(query.text, top_k=top_k)

            try:
                dense_hits = self.dense.search(query.text, top_k=top_k)
            except Exception as exc:  # noqa: BLE001 - degrade explicitly, never silently (F08 s10)
                dense_hits = []
                self.last_degraded_reason = DENSE_UNAVAILABLE_REASON
                attrs["degraded"] = True
                attrs["reason_code"] = DENSE_UNAVAILABLE_REASON
                attrs["error_type"] = type(exc).__name__

            fused = reciprocal_rank_fusion(
                [chunk_id for chunk_id, _ in sparse_hits],
                [chunk_id for chunk_id, _ in dense_hits],
            )
            candidates = tuple(self._to_candidate(entry) for entry in fused)

            attrs["sparse_hit_count"] = len(sparse_hits)
            attrs["dense_hit_count"] = len(dense_hits)
            attrs["fused_candidate_count"] = len(candidates)
            attrs["records"] = [candidate.chunk_id for candidate in candidates]

            return RetrievalResult(
                candidates=candidates,
                corpus_version=self.corpus.corpus_version,
                sparse_hit_count=len(sparse_hits),
                dense_hit_count=len(dense_hits),
                embedding_model_id=self.embedding_model_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

    def _to_candidate(self, entry: FusedEntry) -> Candidate:
        chunk = self.corpus.chunk(entry.chunk_id)
        if chunk is None:  # pragma: no cover - the index is built from this corpus
            raise RetrievalError(f"retrieved chunk {entry.chunk_id!r} is not in the corpus")
        return Candidate(
            chunk_id=chunk.chunk_id,
            text=chunk.text,
            doc_slug=chunk.source_id,
            section=f"{chunk.section_label} - {chunk.section_heading}",
            sparse_rank=entry.sparse_rank,
            dense_rank=entry.dense_rank,
            fused_score=entry.fused_score,
            fused_rank=entry.fused_rank,
        )


def build_retriever(corpus: Corpus | None = None) -> HybridRetriever:
    """The default retriever over the committed corpus."""
    return HybridRetriever(corpus if corpus is not None else build_corpus())


__all__ = [
    "BM25_B",
    "BM25_K1",
    "DEFAULT_TOP_K_PER_RETRIEVER",
    "DENSE_MIN_SIMILARITY",
    "DENSE_UNAVAILABLE_REASON",
    "EMBEDDING_MODEL_ID",
    "MAX_CANDIDATES",
    "RRF_K",
    "CorpusVersionMismatchError",
    "DenseIndex",
    "FusedEntry",
    "HybridRetriever",
    "RetrievalError",
    "Retriever",
    "SparseIndex",
    "build_retriever",
    "cosine",
    "ngram_weights",
    "reciprocal_rank_fusion",
    "tokenize",
]
