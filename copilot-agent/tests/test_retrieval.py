"""Acceptance tests for sparse + dense hybrid retrieval (feature PRD F08).

Written against the PRD's observable behaviour and the frozen seam in
``app.evidence``, not against the retriever's internals:

* F08 s7.1 / s13 AC-1 - an exact term present in a chunk puts that chunk in the
  sparse top-20.
* F08 s7.2 / s13 AC-2 - the dense retriever matches on surface variation the
  keyword index tokenises apart.
* F08 s7.4 / s8 / s13 AC-3 - Reciprocal Rank Fusion with ``k = 60``: a chunk
  found by both retrievers outranks a chunk found by one at the same rank.
* F08 s7.6 / s13 AC-5 - an index/query ``corpus_version`` mismatch is a hard
  error, never a silent stale read.
* F08 s7.8 / s10 - an empty candidate list is a valid, explicit outcome; a
  query with no relevant chunk must not return nearest-neighbour noise.
* F08 s10 / s13 AC-6 - a dense-retriever failure degrades to sparse-only
  without an exception reaching the caller.
* F08 s13 AC-7 - identical query and corpus version produce identical results.
"""

from __future__ import annotations

import pytest

from app.corpus import Corpus, build_corpus
from app.evidence import RetrievalQuery, RetrievalResult
from app.retrieval import (
    EMBEDDING_MODEL_ID,
    MAX_CANDIDATES,
    RRF_K,
    CorpusVersionMismatchError,
    HybridRetriever,
    reciprocal_rank_fusion,
)

# The passages F07 s4 guarantees are in the corpus: individualised glycaemic
# targets live under NDEP Principle 7.
PRINCIPLE_7 = "principle-7-individualize-blood-glucose-management"
PRINCIPLE_5 = "principle-5-encourage-lifestyle-modification"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return build_corpus()


@pytest.fixture(scope="module")
def retriever(corpus: Corpus) -> HybridRetriever:
    return HybridRetriever(corpus)


def query_for(corpus: Corpus, text: str, **overrides: object) -> RetrievalQuery:
    return RetrievalQuery(text=text, corpus_version=corpus.corpus_version, **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The headline case: individualising an A1c target
# --------------------------------------------------------------------------- #


def test_a1c_individualisation_query_surfaces_principle_7(retriever: HybridRetriever, corpus: Corpus) -> None:
    """F08 s13 AC-1/AC-3. The guidance on individualised targets outranks unrelated guidance."""
    result = retriever.retrieve(query_for(corpus, "individualize A1C target glycemic goal for this patient"))

    assert isinstance(result, RetrievalResult)
    assert result.candidates, "the corpus contains guidance on individualised targets"

    top_five = result.candidates[:5]
    assert any(PRINCIPLE_7 in c.chunk_id for c in top_five), [c.chunk_id for c in top_five]

    best_principle_7 = next(c.fused_rank for c in result.candidates if PRINCIPLE_7 in c.chunk_id)
    unrelated = [c for c in result.candidates if PRINCIPLE_5 in c.chunk_id]
    for candidate in unrelated:
        assert best_principle_7 < candidate.fused_rank, (
            f"{candidate.chunk_id} (rank {candidate.fused_rank}) outranked Principle 7 at {best_principle_7}"
        )


def test_the_individualise_goals_passage_is_retrieved(retriever: HybridRetriever, corpus: Corpus) -> None:
    """The specific passage a briefing would cite is reachable, not merely its section."""
    result = retriever.retrieve(query_for(corpus, "individualize glycemic goals shared decision making A1C"))
    texts = [c.text for c in result.candidates]
    assert any("Individualize glycemic goals" in text for text in texts)


# --------------------------------------------------------------------------- #
# Sparse (F08 s7.1, s13 AC-1)
# --------------------------------------------------------------------------- #


def test_exact_term_appears_in_sparse_top_20(retriever: HybridRetriever, corpus: Corpus) -> None:
    ranked = retriever.sparse.search("metformin", top_k=20)
    assert ranked, "metformin is an exact term in the corpus"
    assert len(ranked) <= 20
    corpus = retriever.corpus
    assert all("metformin" in corpus.chunk(chunk_id).text.lower() for chunk_id, _ in ranked)


def test_sparse_scores_are_descending(retriever: HybridRetriever) -> None:
    ranked = retriever.sparse.search("hypoglycemia insulin", top_k=20)
    scores = [score for _, score in ranked]
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------- #
# Dense (F08 s7.2, s13 AC-2)
# --------------------------------------------------------------------------- #


def test_dense_matches_surface_variation_of_a_term(retriever: HybridRetriever) -> None:
    """"individualized glycemic goals" against a passage reading "Individualize glycemic goals".

    The keyword index tokenises the two apart; the character-n-gram embedding
    does not, which is the whole point of running both.
    """
    ranked = retriever.dense.search("individualized glycemic goals", top_k=20)
    assert ranked
    assert any(PRINCIPLE_7 in chunk_id for chunk_id, _ in ranked)


def test_dense_result_count_is_bounded_by_top_k(retriever: HybridRetriever) -> None:
    assert len(retriever.dense.search("blood glucose control", top_k=5)) <= 5


def test_embedding_model_id_is_honest_about_being_local(retriever: HybridRetriever, corpus: Corpus) -> None:
    """F08 s7.2 - the embedding model id is recorded, and it must not imply a hosted model."""
    result = retriever.retrieve(query_for(corpus, "metformin"))
    assert result.embedding_model_id == EMBEDDING_MODEL_ID
    assert "local" in EMBEDDING_MODEL_ID and "deterministic" in EMBEDDING_MODEL_ID


# --------------------------------------------------------------------------- #
# Fusion (F08 s8, s13 AC-3)
# --------------------------------------------------------------------------- #


def test_rrf_k_is_60() -> None:
    assert RRF_K == 60


def test_chunk_found_by_both_outranks_chunk_found_by_one_at_the_same_rank() -> None:
    """F08 s13 AC-3. "both" sits at rank 2 in each list; "sparse-only" leads one list outright."""
    fused = reciprocal_rank_fusion(["sparse-only", "both"], ["dense-only", "both"])
    by_id = {entry.chunk_id: entry for entry in fused}

    assert by_id["both"].fused_rank == 1
    assert by_id["both"].fused_score == pytest.approx(2 / (RRF_K + 2))
    assert by_id["sparse-only"].fused_score == pytest.approx(1 / (RRF_K + 1))
    assert by_id["both"].fused_score > by_id["sparse-only"].fused_score


def test_single_retriever_chunks_still_score_and_carry_a_null_rank() -> None:
    fused = reciprocal_rank_fusion(["a"], ["b"])
    by_id = {entry.chunk_id: entry for entry in fused}

    assert by_id["a"].sparse_rank == 1 and by_id["a"].dense_rank is None
    assert by_id["b"].dense_rank == 1 and by_id["b"].sparse_rank is None
    assert by_id["a"].fused_score == pytest.approx(1 / (RRF_K + 1))


def test_fusion_is_capped_at_thirty_candidates() -> None:
    fused = reciprocal_rank_fusion([f"s{i}" for i in range(40)], [f"d{i}" for i in range(40)])
    assert len(fused) == MAX_CANDIDATES == 30


def test_fused_rank_is_dense_and_gapless_from_one() -> None:
    fused = reciprocal_rank_fusion(["a", "b", "c"], ["c", "d"])
    assert [entry.fused_rank for entry in fused] == [1, 2, 3, 4]


def test_ties_break_deterministically_by_chunk_id() -> None:
    """Equal fused scores must not leave the order to dict iteration."""
    # "b" leads the sparse list, "a" leads the dense list: identical scores.
    fused = reciprocal_rank_fusion(["b", "a"], ["a", "b"])
    assert [e.chunk_id for e in fused] == ["a", "b"]
    assert fused[0].fused_score == pytest.approx(fused[1].fused_score)

    unambiguous = reciprocal_rank_fusion([], ["z", "y"])
    assert unambiguous[0].chunk_id == "z", "rank 1 wins on score, not on alphabet"


# --------------------------------------------------------------------------- #
# Result contract (F08 s8)
# --------------------------------------------------------------------------- #


def test_result_honours_the_frozen_contract(retriever: HybridRetriever, corpus: Corpus) -> None:
    result = retriever.retrieve(query_for(corpus, "A1C testing interval how often"))

    assert result.corpus_version == corpus.corpus_version
    assert len(result.candidates) <= MAX_CANDIDATES
    assert [c.fused_rank for c in result.candidates] == list(range(1, len(result.candidates) + 1))
    assert result.duration_ms >= 0
    for candidate in result.candidates:
        assert candidate.sparse_rank is not None or candidate.dense_rank is not None
        assert corpus.chunk(candidate.chunk_id) is not None
        assert candidate.doc_slug and candidate.section


def test_default_top_k_per_retriever_is_twenty(corpus: Corpus) -> None:
    assert RetrievalQuery(text="x", corpus_version=corpus.corpus_version).top_k_per_retriever == 20


def test_hit_counts_report_what_each_retriever_returned(retriever: HybridRetriever, corpus: Corpus) -> None:
    result = retriever.retrieve(query_for(corpus, "metformin contraindicated", top_k_per_retriever=7))
    assert result.sparse_hit_count <= 7
    assert result.dense_hit_count <= 7
    assert result.sparse_hit_count > 0


# --------------------------------------------------------------------------- #
# Determinism, version pinning, empty results, degradation
# --------------------------------------------------------------------------- #


def test_identical_query_returns_identical_results(retriever: HybridRetriever, corpus: Corpus) -> None:
    """F08 s13 AC-7."""
    first = retriever.retrieve(query_for(corpus, "hypoglycemia risk in older adults"))
    second = retriever.retrieve(query_for(corpus, "hypoglycemia risk in older adults"))
    assert first.model_dump(exclude={"duration_ms"}) == second.model_dump(exclude={"duration_ms"})


def test_version_mismatch_is_a_hard_error(retriever: HybridRetriever) -> None:
    """F08 s13 AC-5. A stale index must never be read silently."""
    stale = RetrievalQuery(text="metformin", corpus_version="v0.deadbeefdeadbeef")
    with pytest.raises(CorpusVersionMismatchError):
        retriever.retrieve(stale)


def test_a_query_with_no_relevant_chunk_returns_an_empty_list_not_noise(
    retriever: HybridRetriever, corpus: Corpus
) -> None:
    """F08 s7.8 / s14 adversarial. Empty is a valid outcome, not an error."""
    result = retriever.retrieve(query_for(corpus, "jabberwocky brillig slithy toves"))
    assert result.candidates == ()
    assert result.sparse_hit_count == 0
    assert result.dense_hit_count == 0
    assert result.corpus_version == corpus.corpus_version


def test_a_stopword_only_query_returns_nothing(retriever: HybridRetriever, corpus: Corpus) -> None:
    result = retriever.retrieve(query_for(corpus, "the"))
    assert result.candidates == ()


def test_a_very_long_query_is_handled(retriever: HybridRetriever, corpus: Corpus) -> None:
    result = retriever.retrieve(query_for(corpus, "A1C glycemic target individualize " * 200))
    assert len(result.candidates) <= MAX_CANDIDATES


def test_dense_failure_degrades_to_sparse_only_without_raising(corpus: Corpus) -> None:
    """F08 s13 AC-6. No exception surfaces; the absence is visible as a zero dense hit count."""

    class ExplodingDense:
        model_id = "exploding"

        def search(self, text: str, *, top_k: int) -> list[tuple[str, float]]:
            raise RuntimeError("embedding service unavailable")

    degraded = HybridRetriever(corpus, dense=ExplodingDense())
    result = degraded.retrieve(query_for(corpus, "metformin"))

    assert result.candidates, "sparse-only results are still returned"
    assert result.dense_hit_count == 0
    assert all(c.dense_rank is None for c in result.candidates)
    assert result.sparse_hit_count > 0
    assert degraded.last_degraded_reason == "dense_retriever_unavailable"


def test_a_healthy_retriever_reports_no_degradation(retriever: HybridRetriever, corpus: Corpus) -> None:
    retriever.retrieve(query_for(corpus, "metformin"))
    assert retriever.last_degraded_reason is None


def test_query_text_is_never_logged(retriever: HybridRetriever, corpus: Corpus, caplog) -> None:
    """F08 s11 - chunk ids, ranks and counts are freely logged; the query is not."""
    secret = "zolpidem"
    with caplog.at_level("DEBUG"):
        retriever.retrieve(query_for(corpus, f"{secret} interaction with metformin"))

    spans = [record for record in caplog.records if getattr(record, "event", "") == "span.retrieval.hybrid"]
    assert spans, "the retrieval span is emitted"
    emitted = spans[-1].__dict__

    assert secret not in caplog.text
    assert secret not in repr(emitted), "the query must not reach the log record's fields either"
    assert emitted["sparse_hit_count"] > 0
    assert emitted["records"], "chunk ids are public-corpus metadata and are logged"
