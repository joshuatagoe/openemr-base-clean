"""Acceptance tests for the guideline corpus (feature PRD F07, ADR-006).

These tests are written against the PRD's observable behaviour, not against the
builder's internals:

* F07 s3.2 - every chunk resolves to title, publisher, year (or retrieval
  date), population scope, evidence tier, section heading and corpus version.
* F07 s3.3 / s7 AC-4, AC-5 - the version is deterministic: unchanged inputs
  rebuild to the same version, one altered word produces a different one.
* F07 s3.5 - the manifest is inspectable and says plainly that this is a
  demonstration artifact, not a clinical reference.
* F07 s3.7 / ADR-006 s4 - tier admissibility is *enforced*. A Tier B passage
  may support a care-process statement and may never support a clinical
  threshold, target or decision boundary.
* F07 s4 - coverage: NDEP Principle 7 (individualised targets), Principle 3
  (barriers including affordability), Principle 4 (DSMES) and the CDC testing
  interval.
* F07 s5 - a chunk missing required metadata fails the build loudly, and a
  passage without provenance is never indexed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.corpus import (
    CORPUS_FIXTURES,
    ClaimKind,
    Corpus,
    CorpusBuildError,
    DropReason,
    EvidenceTier,
    SupportedClaim,
    build_corpus,
    supports,
)

NDEP = "ndep-guiding-principles-2018"
CDC = "cdc-a1c-testing"

# The passage ADR-006 s4 and F07 s9.2 record as inadmissible twice over: a
# population-averaged threshold from a Tier B source. It must never be indexed.
CDC_INADMISSIBLE = "the A1C goal is 7% or less"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return build_corpus()


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    """A writable copy of the committed corpus fixtures."""
    root = tmp_path / "corpus"
    shutil.copytree(CORPUS_FIXTURES, root)
    return root


def _sources_json(root: Path) -> dict:
    return json.loads((root / "sources.json").read_text(encoding="utf-8"))


def _write_sources_json(root: Path, data: dict) -> None:
    (root / "sources.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def _source_spec(data: dict, source_id: str) -> dict:
    return next(s for s in data["sources"] if s["source_id"] == source_id)


def _texts(corpus: Corpus, source_id: str | None = None) -> list[str]:
    chunks = corpus.chunks if source_id is None else corpus.chunks_for(source_id)
    return [chunk.text for chunk in chunks]


def _chunk_containing(corpus: Corpus, needle: str, source_id: str | None = None):
    chunks = corpus.chunks if source_id is None else corpus.chunks_for(source_id)
    matches = [chunk for chunk in chunks if needle in chunk.text]
    assert matches, f"no chunk contains {needle!r}"
    return matches[0]


# --------------------------------------------------------------------------- #
# The corpus contains the two decided sources (F07 s3.1, ADR-006 s2)
# --------------------------------------------------------------------------- #


def test_corpus_holds_exactly_the_two_decided_sources(corpus: Corpus) -> None:
    assert [source.source_id for source in corpus.sources] == [NDEP, CDC]


def test_source_tiers_match_adr_006(corpus: Corpus) -> None:
    tiers = {source.source_id: source.evidence_tier for source in corpus.sources}
    assert tiers[NDEP] is EvidenceTier.A
    assert tiers[CDC] is EvidenceTier.B


def test_both_sources_are_indexed_into_chunks(corpus: Corpus) -> None:
    assert corpus.chunks_for(NDEP)
    assert corpus.chunks_for(CDC)


def test_ndep_is_indexed_at_the_planned_granularity(corpus: Corpus) -> None:
    """F07 s9.5 plans "roughly 80-120" passages so retrieval does real work.

    The floor is what the requirement protects: too few passages and retrieval
    has nothing to discriminate between. The document's own structure sets the
    actual count - it has 93 headed sub-sections, and a passage is a run of
    same-kind blocks under one heading - so the upper bound here is a
    sanity check, paired with two shape checks that a count alone would miss:
    no passage exceeds the configured maximum, and the corpus is not shredded
    into fragments too small to read as guidance.
    """
    chunks = corpus.chunks_for(NDEP)
    assert 80 <= len(chunks) <= 250

    max_chars = corpus.chunking["max_chunk_chars"]
    assert all(len(chunk.text) <= max_chars for chunk in chunks)

    lengths = sorted(len(chunk.text) for chunk in chunks)
    assert lengths[len(lengths) // 2] >= 200


# --------------------------------------------------------------------------- #
# Every chunk carries full provenance (F07 s3.2, s3.4, AC-1, AC-2)
# --------------------------------------------------------------------------- #


def test_every_chunk_resolves_to_full_provenance(corpus: Corpus) -> None:
    for chunk in corpus.chunks:
        citation = corpus.citation_for(chunk.chunk_id)
        assert citation.document_title
        assert citation.publisher
        assert citation.publication_year is not None or citation.retrieved is not None
        assert citation.population_scope
        assert citation.evidence_tier in (EvidenceTier.A, EvidenceTier.B)
        assert citation.section_heading
        assert citation.section_label
        assert citation.corpus_version == corpus.corpus_version
        assert citation.quote == chunk.text


#: The passages F07 s9.1 and s9.2 quote verbatim as the corpus's reason to exist.
PRD_VERBATIM_PASSAGES = (
    (NDEP, "Treatment targets should be individualized based on duration of diabetes"),
    (NDEP, "Individualize glycemic goals based on the characteristics and preferences"),
    (NDEP, "Moderate A1C goals, such as < 8 percent, are appropriate for persons"),
    (NDEP, "Reassess A1C targets, patient preferences, and treatment strategies over time"),
    (NDEP, "Sensitive assessment of a person's ability to afford office visits"),
    (NDEP, "Motivational interviewing may help address individual barriers"),
    (CDC, "Most people with diabetes have their A1C tested at least twice a year."),
)


@pytest.mark.parametrize(("source_id", "passage"), PRD_VERBATIM_PASSAGES)
def test_indexed_passages_are_verbatim_not_paraphrased(corpus: Corpus, source_id: str, passage: str) -> None:
    """A citation quotes the corpus; the corpus quotes the committed text file.

    The build may rewrap lines and drop the PDF's bullet glyphs and running
    headers. It may never restate the source in different words, so each
    passage the PRD relies on must survive chunking character for character.
    """
    source = next(s for s in corpus.sources if s.source_id == source_id)
    source_text = (CORPUS_FIXTURES / source.text_file).read_text(encoding="utf-8")
    flattened = " ".join(source_text.replace("•", " ").split())

    assert passage in flattened, "the passage is not in the committed source text"
    assert any(passage in chunk.text for chunk in corpus.chunks_for(source_id))


def test_citation_records_the_kind_of_passage_it_quotes(corpus: Corpus) -> None:
    """F07 s11 - a narrative sentence must not read as a graded recommendation."""
    kinds = {chunk.passage_kind for chunk in corpus.chunks_for(NDEP)}
    assert {"recommendation_bullet", "narrative"} <= kinds


def test_chunk_ids_are_unique(corpus: Corpus) -> None:
    ids = [chunk.chunk_id for chunk in corpus.chunks]
    assert len(ids) == len(set(ids))


def test_every_chunk_names_its_source(corpus: Corpus) -> None:
    assert {chunk.source_id for chunk in corpus.chunks} == {NDEP, CDC}


def test_unknown_chunk_id_does_not_resolve(corpus: Corpus) -> None:
    assert corpus.chunk("no-such-chunk") is None
    with pytest.raises(KeyError):
        corpus.citation_for("no-such-chunk")


# --------------------------------------------------------------------------- #
# Determinism and idempotence (F07 s3.3, s3.6, AC-4, AC-5)
# --------------------------------------------------------------------------- #


def test_rebuild_from_unchanged_inputs_is_idempotent(corpus: Corpus) -> None:
    rebuilt = build_corpus()
    assert rebuilt.corpus_version == corpus.corpus_version
    assert [c.chunk_id for c in rebuilt.chunks] == [c.chunk_id for c in corpus.chunks]
    assert [c.text for c in rebuilt.chunks] == [c.text for c in corpus.chunks]


def test_copied_inputs_rebuild_to_the_same_version(corpus: Corpus, corpus_root: Path) -> None:
    assert build_corpus(corpus_root).corpus_version == corpus.corpus_version


def test_a_single_altered_word_changes_the_version(corpus: Corpus, corpus_root: Path) -> None:
    text_file = corpus_root / "ndep-guiding-principles-2018.txt"
    original = text_file.read_text(encoding="utf-8")
    altered = original.replace("Reassess A1C targets", "Revisit A1C targets", 1)
    assert altered != original
    text_file.write_text(altered, encoding="utf-8")

    assert build_corpus(corpus_root).corpus_version != corpus.corpus_version


def test_changing_chunking_parameters_changes_the_version(corpus: Corpus, corpus_root: Path) -> None:
    data = _sources_json(corpus_root)
    data["chunking"]["max_chunk_chars"] = int(data["chunking"]["max_chunk_chars"]) - 100
    _write_sources_json(corpus_root, data)

    assert build_corpus(corpus_root).corpus_version != corpus.corpus_version


def test_version_is_stable_under_reformatting_of_the_spec_file(corpus: Corpus, corpus_root: Path) -> None:
    """The version tracks content and chunking, not JSON whitespace."""
    data = _sources_json(corpus_root)
    (corpus_root / "sources.json").write_text(json.dumps(data, indent=8), encoding="utf-8")

    assert build_corpus(corpus_root).corpus_version == corpus.corpus_version


# --------------------------------------------------------------------------- #
# The manifest (F07 s3.5, AC-1)
# --------------------------------------------------------------------------- #


def test_manifest_states_it_is_a_demonstration_artifact(corpus: Corpus) -> None:
    disclaimer = corpus.manifest()["disclaimer"].lower()
    assert "demonstration artifact" in disclaimer
    assert "not a clinical reference" in disclaimer


def test_manifest_is_json_serialisable_and_carries_the_version(corpus: Corpus) -> None:
    manifest = corpus.manifest()
    assert json.loads(json.dumps(manifest))["corpus_version"] == corpus.corpus_version


def test_manifest_lists_every_required_field_per_source(corpus: Corpus) -> None:
    for entry in corpus.manifest()["sources"]:
        assert entry["title"]
        assert entry["publisher"]
        assert entry["publication_year"] is not None or entry["retrieved"]
        assert entry["population_scope"]
        assert entry["evidence_tier"] in ("A", "B")
        assert entry["section_structure"]
        assert entry["licence_notice"]
        assert entry["licence_basis"]
        assert entry["source_url"].startswith("https://")
        assert len(entry["text_sha256"]) == 64
        assert entry["chunk_count"] > 0


def test_manifest_quotes_the_ndep_permission_notice_verbatim(corpus: Corpus) -> None:
    entry = next(e for e in corpus.manifest()["sources"] if e["source_id"] == NDEP)
    assert entry["licence_notice"] == (
        "This information is not copyrighted. The NIDDK encourages people to share this content freely."
    )
    assert entry["source_sha256"] == "40a7da28c6889a262f4fb20e6ed648de3e7d55c0aab337491721bd232e7c2aa9"


def test_manifest_records_the_deliberate_cdc_exclusion(corpus: Corpus) -> None:
    """F07 s9.2 - the exclusion is reviewable, not an oversight."""
    entry = next(e for e in corpus.manifest()["sources"] if e["source_id"] == CDC)
    excluded = entry["excluded_passages"]
    assert excluded
    assert any(CDC_INADMISSIBLE in item["text"] for item in excluded)
    assert all(item["reason"] for item in excluded)


# --------------------------------------------------------------------------- #
# Coverage (F07 s4)
# --------------------------------------------------------------------------- #


def test_covers_t2_individualised_a1c_targets(corpus: Corpus) -> None:
    chunk = _chunk_containing(corpus, "Treatment targets should be individualized", NDEP)
    assert chunk.section_label.startswith("Principle 7")
    assert corpus.citation_for(chunk.chunk_id).evidence_tier is EvidenceTier.A


def test_covers_t2_conditioned_numeric_targets(corpus: Corpus) -> None:
    lower = _chunk_containing(corpus, "Consider an A1C < 7", NDEP)
    moderate = _chunk_containing(corpus, "Moderate A1C goals, such as < 8 percent", NDEP)
    for chunk in (lower, moderate):
        assert chunk.section_label.startswith("Principle 7")
    # ADR-006 s3: every NDEP target is conditioned on patient characteristics,
    # never on population frequency.
    assert "sufficiently long-life expectancy" in lower.text or "sufficiently long life expectancy" in lower.text
    assert "severe hypoglycemia" in moderate.text


def test_covers_t3_reassessment(corpus: Corpus) -> None:
    chunk = _chunk_containing(corpus, "Reassess A1C targets", NDEP)
    assert chunk.section_label.startswith("Principle 7")


def test_covers_t4_affordability_barriers(corpus: Corpus) -> None:
    chunk = _chunk_containing(corpus, "ability to afford office visits", NDEP)
    assert chunk.section_label.startswith("Principle 3")


def test_covers_principle_4_dsmes(corpus: Corpus) -> None:
    chunk = _chunk_containing(corpus, "socioeconomic barriers to diabetes self-management", NDEP)
    assert chunk.section_label.startswith("Principle 4")
    assert any("DSMES" in text for text in _texts(corpus, NDEP))


def test_covers_t1_cdc_testing_interval(corpus: Corpus) -> None:
    chunk = _chunk_containing(corpus, "at least twice a year", CDC)
    citation = corpus.citation_for(chunk.chunk_id)
    assert citation.evidence_tier is EvidenceTier.B
    assert citation.publication_year is None
    assert citation.retrieved is not None


def test_all_ten_ndep_principles_are_indexed(corpus: Corpus) -> None:
    labels = {c.section_label.split(":")[0] for c in corpus.chunks_for(NDEP)}
    for number in range(1, 11):
        assert f"Principle {number}" in labels


def test_cross_source_overlap_is_retained_not_resolved(corpus: Corpus) -> None:
    """F07 s5 - disagreement is surfaced at answer time, never at corpus level."""
    a1c_chunks = [c for c in corpus.chunks if "A1C" in c.text]
    assert {c.source_id for c in a1c_chunks} == {NDEP, CDC}


# --------------------------------------------------------------------------- #
# The inadmissible CDC passage is never indexed (F07 s9.2)
# --------------------------------------------------------------------------- #


def test_cdc_population_averaged_threshold_is_not_indexed(corpus: Corpus) -> None:
    assert not any(CDC_INADMISSIBLE in text for text in _texts(corpus))


def test_indexing_an_excluded_passage_fails_the_build(corpus_root: Path) -> None:
    text_file = corpus_root / "cdc-a1c-testing.txt"
    text_file.write_text(
        text_file.read_text(encoding="utf-8")
        + "\nFor most people with diabetes, the A1C goal is 7% or less, which is a threshold claim.\n",
        encoding="utf-8",
    )
    with pytest.raises(CorpusBuildError, match="excluded passage"):
        build_corpus(corpus_root)


# --------------------------------------------------------------------------- #
# Tier admissibility is enforced, not advisory (F07 s3.7, AC-6, AC-7)
# --------------------------------------------------------------------------- #


def test_tier_a_supports_both_claim_kinds() -> None:
    assert supports(EvidenceTier.A, ClaimKind.CARE_PROCESS) is True
    assert supports(EvidenceTier.A, ClaimKind.THRESHOLD) is True


def test_tier_b_supports_a_care_process_claim() -> None:
    assert supports(EvidenceTier.B, ClaimKind.CARE_PROCESS) is True


def test_tier_b_never_supports_a_threshold_claim() -> None:
    assert supports(EvidenceTier.B, ClaimKind.THRESHOLD) is False


def test_threshold_claim_supported_only_by_tier_b_is_dropped_and_counted(corpus: Corpus) -> None:
    cdc_chunk = _chunk_containing(corpus, "at least twice a year", CDC)
    claim = SupportedClaim(
        claim_id="target-from-patient-education",
        claim_kind=ClaimKind.THRESHOLD,
        supporting_chunk_ids=[cdc_chunk.chunk_id],
    )

    result = corpus.screen_claims([claim])

    assert result.kept == ()
    assert result.dropped_count == 1
    assert result.dropped[0].claim_id == "target-from-patient-education"
    assert result.dropped[0].reason is DropReason.TIER_INADMISSIBLE


def test_care_process_claim_supported_by_tier_b_survives_with_its_tier_visible(corpus: Corpus) -> None:
    cdc_chunk = _chunk_containing(corpus, "at least twice a year", CDC)
    claim = SupportedClaim(
        claim_id="testing-interval",
        claim_kind=ClaimKind.CARE_PROCESS,
        supporting_chunk_ids=[cdc_chunk.chunk_id],
    )

    result = corpus.screen_claims([claim])

    assert result.dropped_count == 0
    (kept,) = result.kept
    assert kept.claim_id == "testing-interval"
    assert [c.evidence_tier for c in kept.citations] == [EvidenceTier.B]


def test_threshold_claim_with_tier_a_support_survives_without_the_tier_b_passage(corpus: Corpus) -> None:
    tier_a = _chunk_containing(corpus, "Moderate A1C goals, such as < 8 percent", NDEP)
    tier_b = _chunk_containing(corpus, "at least twice a year", CDC)
    claim = SupportedClaim(
        claim_id="which-target-applies",
        claim_kind=ClaimKind.THRESHOLD,
        supporting_chunk_ids=[tier_a.chunk_id, tier_b.chunk_id],
    )

    result = corpus.screen_claims([claim])

    assert result.dropped_count == 0
    (kept,) = result.kept
    assert [c.chunk_id for c in kept.citations] == [tier_a.chunk_id]


def test_claim_whose_citation_cannot_be_resolved_is_dropped(corpus: Corpus) -> None:
    claim = SupportedClaim(
        claim_id="dangling",
        claim_kind=ClaimKind.CARE_PROCESS,
        supporting_chunk_ids=["ndep-guiding-principles-2018::gone::001"],
    )

    result = corpus.screen_claims([claim])

    assert result.kept == ()
    assert result.dropped_count == 1
    assert result.dropped[0].reason is DropReason.UNRESOLVABLE_CITATION


def test_claim_with_no_support_is_dropped(corpus: Corpus) -> None:
    claim = SupportedClaim(claim_id="uncited", claim_kind=ClaimKind.CARE_PROCESS, supporting_chunk_ids=[])

    result = corpus.screen_claims([claim])

    assert result.dropped_count == 1
    assert result.dropped[0].reason is DropReason.UNRESOLVABLE_CITATION


def test_screening_reports_every_claim_exactly_once(corpus: Corpus) -> None:
    tier_a = _chunk_containing(corpus, "Reassess A1C targets", NDEP)
    tier_b = _chunk_containing(corpus, "at least twice a year", CDC)
    claims = [
        SupportedClaim(claim_id="a", claim_kind=ClaimKind.THRESHOLD, supporting_chunk_ids=[tier_a.chunk_id]),
        SupportedClaim(claim_id="b", claim_kind=ClaimKind.THRESHOLD, supporting_chunk_ids=[tier_b.chunk_id]),
        SupportedClaim(claim_id="c", claim_kind=ClaimKind.CARE_PROCESS, supporting_chunk_ids=[tier_b.chunk_id]),
    ]

    result = corpus.screen_claims(claims)

    assert [k.claim_id for k in result.kept] == ["a", "c"]
    assert [d.claim_id for d in result.dropped] == ["b"]
    assert result.dropped_count == 1


# --------------------------------------------------------------------------- #
# A chunk missing required metadata fails the build loudly (F07 s5)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", ["publisher", "population_scope", "evidence_tier", "licence_notice", "title"])
def test_missing_required_source_metadata_fails_the_build(corpus_root: Path, field: str) -> None:
    data = _sources_json(corpus_root)
    _source_spec(data, NDEP).pop(field)
    _write_sources_json(corpus_root, data)

    with pytest.raises(CorpusBuildError, match=field):
        build_corpus(corpus_root)


def test_missing_both_year_and_retrieval_date_fails_the_build(corpus_root: Path) -> None:
    data = _sources_json(corpus_root)
    spec = _source_spec(data, CDC)
    spec["publication_year"] = None
    spec["retrieved"] = None
    _write_sources_json(corpus_root, data)

    with pytest.raises(CorpusBuildError, match="publication_year"):
        build_corpus(corpus_root)


def test_unknown_evidence_tier_fails_the_build(corpus_root: Path) -> None:
    data = _sources_json(corpus_root)
    _source_spec(data, CDC)["evidence_tier"] = "C"
    _write_sources_json(corpus_root, data)

    with pytest.raises(CorpusBuildError, match="evidence_tier"):
        build_corpus(corpus_root)


def test_missing_text_file_fails_the_build(corpus_root: Path) -> None:
    (corpus_root / "cdc-a1c-testing.txt").unlink()

    with pytest.raises(CorpusBuildError, match="cdc-a1c-testing.txt"):
        build_corpus(corpus_root)


def test_source_that_yields_no_passages_fails_the_build(corpus_root: Path) -> None:
    (corpus_root / "cdc-a1c-testing.txt").write_text("\n\n", encoding="utf-8")

    with pytest.raises(CorpusBuildError, match="no passages"):
        build_corpus(corpus_root)


def test_missing_disclaimer_fails_the_build(corpus_root: Path) -> None:
    data = _sources_json(corpus_root)
    data.pop("disclaimer")
    _write_sources_json(corpus_root, data)

    with pytest.raises(CorpusBuildError, match="disclaimer"):
        build_corpus(corpus_root)
