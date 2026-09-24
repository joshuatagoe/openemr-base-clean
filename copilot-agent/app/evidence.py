"""The retrieval -> briefing seam, transcribed from F08 §8 and F09 §8.

Defined before either side is built so two implementers can work in parallel
against one vocabulary. The field names and the status enum come from the
feature PRDs, not from this file's author; do not widen either without
amending the PRD first.

Note on citations: F09 says `citation: Citation  # shape owned by F05`. F05 is
not built, and three citation types currently exist (Week 1's
`contracts.Citation`, `corpus.Citation`, `documents.DocumentCitation`). Only
`DocumentCitation` satisfies CR5's five required fields; `corpus.Citation`
does not. `GuidelineCitation` below is the CR5-shaped adapter for guideline
sources, so the briefing emits a compliant citation without refactoring the
corpus. Unifying all four under F05 is recorded as debt.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from app.contracts import StrictModel
from app.corpus import Citation as CorpusCitation
from app.corpus import EvidenceTier


class RetrievalStatus(StrEnum):
    """Three states, not a boolean.

    "degraded" and "unavailable" both yield few or no snippets but mean
    opposite things to a physician: one is "the system is impaired", the other
    is "guidance does not address this". F11 must be able to say which.
    """

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class RetrievalQuery(StrictModel):
    """F08 §8. `text` is minimised clinical concepts, never the raw record."""

    text: str = Field(min_length=1)
    top_k_per_retriever: int = Field(default=20, ge=1)
    corpus_version: str = Field(min_length=1)


class Candidate(StrictModel):
    """F08 §8. Ranks are 1-based; a null rank means that retriever missed it."""

    chunk_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    doc_slug: str = Field(min_length=1)
    section: str = Field(min_length=1)
    sparse_rank: int | None = Field(default=None, ge=1)
    dense_rank: int | None = Field(default=None, ge=1)
    fused_score: float
    fused_rank: int = Field(ge=1)

    @model_validator(mode="after")
    def _found_by_something(self) -> Candidate:
        if self.sparse_rank is None and self.dense_rank is None:
            raise ValueError("a candidate must have been found by at least one retriever")
        return self


class RetrievalResult(StrictModel):
    """F08 §8. `corpus_version` must equal the loaded index version."""

    candidates: tuple[Candidate, ...] = ()
    corpus_version: str = Field(min_length=1)
    sparse_hit_count: int = Field(ge=0)
    dense_hit_count: int = Field(ge=0)
    embedding_model_id: str
    duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _fused_rank_is_dense_and_gapless(self) -> RetrievalResult:
        ranks = sorted(c.fused_rank for c in self.candidates)
        if ranks and ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("fused_rank must be dense and gapless, starting at 1")
        return self


class GuidelineCitation(StrictModel):
    """CR5's five required fields, for a guideline source.

    Adapter rather than a replacement: `corpus.Citation` carries the provenance
    the corpus build verified (population scope, tier, licence), and this
    reshapes it into what CR5 requires of every displayed claim.
    """

    source_type: Literal["guideline"] = "guideline"
    source_id: str = Field(min_length=1, description="chunk_id.")
    page_or_section: str = Field(min_length=1, description="Section label and heading.")
    field_or_chunk_id: str = Field(min_length=1)
    quote_or_value: str = Field(min_length=1, description="Verbatim passage text.")

    corpus_version: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    publication_year: int | None = None
    population_scope: str = Field(min_length=1, description="Who the guidance was written about. Displayed.")
    evidence_tier: EvidenceTier = Field(description="Displayed. Tier B may not support a threshold claim.")

    @classmethod
    def from_corpus(cls, c: CorpusCitation) -> GuidelineCitation:
        return cls(
            source_id=c.chunk_id,
            page_or_section=f"{c.section_label} - {c.section_heading}",
            field_or_chunk_id=c.chunk_id,
            quote_or_value=c.quote,
            corpus_version=c.corpus_version,
            publisher=c.publisher,
            publication_year=c.publication_year,
            population_scope=c.population_scope,
            evidence_tier=c.evidence_tier,
        )


class EvidenceSnippet(StrictModel):
    """F09 §8. Satisfies W2-DATA-013."""

    chunk_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    doc_title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    section: str = Field(min_length=1)
    relevance_score: float
    citation: GuidelineCitation


class EvidencePackage(StrictModel):
    """F09 §8 - what F11 consumes, and the ONLY thing the answer model sees.

    CR3: "feed only the top grounded evidence to the answer model." The
    briefing may not reach past this into the corpus, so "what evidence did
    this answer see?" has exactly one answer, and it is the thing logged.
    """

    snippets: tuple[EvidenceSnippet, ...] = ()
    corpus_version: str = Field(min_length=1)
    reranker_model_id: str
    status: RetrievalStatus = RetrievalStatus.OK
    degraded_reason: str | None = Field(
        default=None, description="Fixed string. Never a raw exception, never patient-specific."
    )

    @model_validator(mode="after")
    def _status_matches_contents(self) -> EvidencePackage:
        if self.status is RetrievalStatus.OK and not self.snippets:
            raise ValueError('status "ok" requires at least one snippet')
        if self.status is RetrievalStatus.UNAVAILABLE:
            if self.snippets:
                raise ValueError('status "unavailable" requires an empty snippet list')
            if not self.degraded_reason:
                raise ValueError('status "unavailable" requires a reason')
        return self

    def tiers_present(self) -> set[EvidenceTier]:
        return {s.citation.evidence_tier for s in self.snippets}


@runtime_checkable
class Reranker(Protocol):
    """ADR-002 boundary. Bedrock/boto3 types appear ONLY in the adapter.

    A fake must satisfy every non-live test the real one does, or the offline
    CI gate passes for the wrong reason.
    """

    name: str

    def rerank(self, query: str, candidates: tuple[Candidate, ...], *, top_k: int = 5) -> EvidencePackage:
        ...


__all__ = [
    "Candidate",
    "EvidencePackage",
    "EvidenceSnippet",
    "GuidelineCitation",
    "Reranker",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievalStatus",
]
