"""Acceptance tests for reranking and evidence packaging (feature PRD F09, ADR-002).

The load-bearing test in this file is the parametrised one. `F09 s16 (L)`:
"``FakeReranker`` must satisfy every non-live test the Bedrock adapter
satisfies, or the key-free grader gate proves nothing." So every behavioural
assertion below runs against **both** implementations - the Bedrock adapter
driven through an injected stub client that speaks the real response shape -
and only the two adapter-specific tests at the end are single-implementation.

Also covered:

* F09 s7.6 / s13 AC-1 - at most ``top_k = 5`` snippets, ordered by relevance.
* F09 s7.7 / s13 AC-1 / AC-5 - every snippet carries a resolvable guideline
  citation, population scope and evidence tier included.
* F09 s7.8 / s10 / s13 AC-3 - a rerank failure is an explicit ``unavailable``
  state with a fixed reason. Never an exception, and never a silent pass-through
  of unreranked candidates.
* F09 s13 AC-2 - reranking reorders: a chunk fusion put outside the top 5
  reaches the top 5.
* F09 s13 AC-4 - the fake is deterministic.
* F09 s13 AC-7 / s16 (D) - ``boto3`` is imported in exactly one module.
"""

from __future__ import annotations

import json

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

from app.corpus import Corpus, EvidenceTier, build_corpus
from app.evidence import Candidate, EvidencePackage, Reranker, RetrievalQuery, RetrievalStatus
from app.providers.base import ProviderConfigurationError
from app.reranker import (
    BEDROCK_RERANK_MODEL_ID,
    DEFAULT_TOP_K,
    NO_CANDIDATES_REASON,
    RERANKER_UNAVAILABLE_REASON,
    BedrockReranker,
    FakeReranker,
)
from app.retrieval import HybridRetriever

APP_DIR = Path(__file__).resolve().parent.parent / "app"


# --------------------------------------------------------------------------- #
# Fixtures: a corpus, real candidates, and a stub that speaks Bedrock's shape
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return build_corpus()


@pytest.fixture(scope="module")
def candidates(corpus: Corpus) -> tuple[Candidate, ...]:
    retriever = HybridRetriever(corpus)
    result = retriever.retrieve(
        RetrievalQuery(
            text="individualize A1C target glycemic goal older adult hypoglycemia",
            corpus_version=corpus.corpus_version,
        )
    )
    assert len(result.candidates) > DEFAULT_TOP_K, "the rerank tests need more candidates than top_k"
    return result.candidates


class StubBedrockClient:
    """Speaks the ``bedrock-agent-runtime`` Rerank request and response shape.

    Scores in reverse candidate order, so a package that merely echoed the fused
    order would fail the ordering assertions rather than pass them by accident.
    """

    def __init__(self, *, error: Exception | None = None, results: list[dict] | None = None) -> None:
        self.error = error
        self.results = results
        self.calls: list[dict] = []

    def rerank(self, **request: object) -> dict:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        if self.results is not None:
            return {"results": self.results}
        sources = request["sources"]
        configuration = request["rerankingConfiguration"]["bedrockRerankingConfiguration"]  # type: ignore[index]
        wanted = min(int(configuration["numberOfResults"]), len(sources))  # type: ignore[arg-type,index]
        order = list(reversed(range(len(sources))))[:wanted]  # type: ignore[arg-type]
        return {"results": [{"index": i, "relevanceScore": 0.99 - n * 0.01} for n, i in enumerate(order)]}


RerankerFactory = Callable[..., Reranker]


def _fake(corpus: Corpus, **kwargs: object) -> Reranker:
    return FakeReranker(corpus, **kwargs)  # type: ignore[arg-type]


def _bedrock(corpus: Corpus, *, available: bool = True, **kwargs: object) -> Reranker:
    client = StubBedrockClient(error=None if available else RuntimeError("bedrock is down"))
    return BedrockReranker(corpus, client=client, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(params=["fake", "bedrock"])
def make_reranker(request: pytest.FixtureRequest) -> RerankerFactory:
    """Both implementations, behind one callable. F09 s16 (L)."""
    return _fake if request.param == "fake" else _bedrock


@pytest.fixture
def reranker(make_reranker: RerankerFactory, corpus: Corpus) -> Reranker:
    return make_reranker(corpus)


QUERY = "individualized A1C target for an older adult at risk of hypoglycemia"


# --------------------------------------------------------------------------- #
# Protocol conformance, run against both implementations
# --------------------------------------------------------------------------- #


def test_satisfies_the_reranker_protocol(reranker: Reranker) -> None:
    assert isinstance(reranker, Reranker)
    assert isinstance(reranker.name, str) and reranker.name


def test_returns_at_most_top_k_snippets(reranker: Reranker, candidates: tuple[Candidate, ...]) -> None:
    """F09 s7.6 / s13 AC-1."""
    package = reranker.rerank(QUERY, candidates)
    assert isinstance(package, EvidencePackage)
    assert package.status is RetrievalStatus.OK
    assert 1 <= len(package.snippets) <= DEFAULT_TOP_K == 5


def test_top_k_is_configurable_and_respected(reranker: Reranker, candidates: tuple[Candidate, ...]) -> None:
    assert len(reranker.rerank(QUERY, candidates, top_k=2).snippets) == 2
    assert len(reranker.rerank(QUERY, candidates, top_k=1).snippets) == 1


def test_snippets_are_ordered_by_descending_relevance(reranker: Reranker, candidates: tuple[Candidate, ...]) -> None:
    scores = [snippet.relevance_score for snippet in reranker.rerank(QUERY, candidates).snippets]
    assert scores == sorted(scores, reverse=True)


def test_every_snippet_carries_a_complete_guideline_citation(
    reranker: Reranker, candidates: tuple[Candidate, ...], corpus: Corpus
) -> None:
    """F09 s7.7 / s8 - W2-DATA-013, and CR5's displayed fields."""
    package = reranker.rerank(QUERY, candidates)
    assert package.snippets

    for snippet in package.snippets:
        citation = snippet.citation
        assert citation.source_type == "guideline"
        assert citation.source_id == snippet.chunk_id == citation.field_or_chunk_id
        assert citation.corpus_version == corpus.corpus_version == package.corpus_version
        assert citation.quote_or_value == snippet.text
        assert citation.page_or_section.strip()
        assert citation.publisher.strip() == snippet.publisher.strip()
        # The two fields ADR-006 insists are displayed, not merely stored.
        assert citation.population_scope.strip()
        assert isinstance(citation.evidence_tier, EvidenceTier)
        assert snippet.doc_title.strip() and snippet.section.strip()


def test_snippet_text_matches_the_corpus_verbatim(
    reranker: Reranker, candidates: tuple[Candidate, ...], corpus: Corpus
) -> None:
    for snippet in reranker.rerank(QUERY, candidates).snippets:
        chunk = corpus.chunk(snippet.chunk_id)
        assert chunk is not None
        assert snippet.text == chunk.text


def test_reranking_is_deterministic(reranker: Reranker, candidates: tuple[Candidate, ...]) -> None:
    """F09 s13 AC-4."""
    first = reranker.rerank(QUERY, candidates)
    second = reranker.rerank(QUERY, candidates)
    assert first.model_dump() == second.model_dump()


def test_no_candidates_is_not_reported_as_ok(reranker: Reranker) -> None:
    """The frozen contract forbids ``ok`` with no snippets, so "nothing matched" must say so."""
    package = reranker.rerank(QUERY, ())
    assert package.snippets == ()
    assert package.status is not RetrievalStatus.OK
    assert package.degraded_reason == NO_CANDIDATES_REASON


def test_a_single_candidate_is_handled(reranker: Reranker, candidates: tuple[Candidate, ...]) -> None:
    """F09 s14 adversarial."""
    package = reranker.rerank(QUERY, candidates[:1])
    assert len(package.snippets) == 1
    assert package.status is RetrievalStatus.OK


def test_failure_degrades_to_unavailable_without_raising(
    make_reranker: RerankerFactory, corpus: Corpus, candidates: tuple[Candidate, ...]
) -> None:
    """F09 s7.8 / s13 AC-3. Never an exception, never unreranked candidates passed through."""
    broken = make_reranker(corpus, available=False)
    package = broken.rerank(QUERY, candidates)

    assert package.status is RetrievalStatus.UNAVAILABLE
    assert package.snippets == ()
    assert package.degraded_reason == RERANKER_UNAVAILABLE_REASON
    assert package.corpus_version == corpus.corpus_version


def test_the_unavailable_reason_is_a_fixed_string_not_an_exception(
    make_reranker: RerankerFactory, corpus: Corpus, candidates: tuple[Candidate, ...]
) -> None:
    """The reason is displayed; a raw exception string could carry anything."""
    broken = make_reranker(corpus, available=False)
    reason = broken.rerank(QUERY, candidates).degraded_reason or ""
    assert reason == RERANKER_UNAVAILABLE_REASON
    assert reason.islower() and " " not in reason
    assert "bedrock is down" not in reason and "Error" not in reason


def test_the_model_id_is_recorded_on_every_package(reranker: Reranker, candidates: tuple[Candidate, ...]) -> None:
    """F09 s7.2 - the model id is pinned and logged per call."""
    package = reranker.rerank(QUERY, candidates)
    assert package.reranker_model_id
    assert package.reranker_model_id == reranker.model_id  # type: ignore[attr-defined]


def test_query_text_is_never_logged(reranker: Reranker, candidates: tuple[Candidate, ...], caplog) -> None:
    """F09 s11 / s13 AC-6 - chunk ids and scores are logged; the query is not."""
    secret = "zolpidem"
    with caplog.at_level("DEBUG"):
        reranker.rerank(f"{secret} and A1C targets", candidates)

    spans = [record for record in caplog.records if getattr(record, "event", "") == "span.rerank"]
    assert spans, "the rerank span is emitted"
    emitted = spans[-1].__dict__

    assert secret not in caplog.text
    assert secret not in repr(emitted), "the query must not reach the log record's fields either"
    assert emitted["selected_chunk_ids"], "chunk ids are public-corpus metadata and are logged"
    assert emitted["relevance_scores"]
    assert emitted["candidate_count"] == len(candidates)


# --------------------------------------------------------------------------- #
# Reranking must actually reorder (F09 s4.2, s13 AC-2)
# --------------------------------------------------------------------------- #


def test_rerank_promotes_a_chunk_fusion_ranked_outside_the_top_five(corpus: Corpus) -> None:
    """The stage earns its place only if it moves something."""
    target_id = next(c.chunk_id for c in corpus.chunks if "principle-7" in c.chunk_id and "::012" in c.chunk_id)
    target = corpus.chunk(target_id)
    assert target is not None and "Individualize glycemic goals" in target.text

    filler = [c for c in corpus.chunks if "principle-5" in c.chunk_id][:7]
    ordered = [*filler, target]
    built = tuple(
        Candidate(
            chunk_id=chunk.chunk_id,
            text=chunk.text,
            doc_slug=chunk.source_id,
            section=f"{chunk.section_label} - {chunk.section_heading}",
            sparse_rank=position,
            dense_rank=None,
            fused_score=1.0 / (60 + position),
            fused_rank=position,
        )
        for position, chunk in enumerate(ordered, start=1)
    )
    assert built[-1].fused_rank == 8

    package = FakeReranker(corpus).rerank("individualize glycemic goals shared decision making", built)
    selected = [snippet.chunk_id for snippet in package.snippets]
    assert target_id in selected, selected
    assert selected[0] == target_id


# --------------------------------------------------------------------------- #
# Adapter-specific: the Bedrock boundary (ADR-002 s10.1, F09 s16 (D))
# --------------------------------------------------------------------------- #


AWS_SDK_PACKAGES = {"boto3", "botocore"}


def sdk_import_nodes(path: Path) -> list[ast.stmt]:
    """Every statement in ``path`` that imports the AWS SDK.

    Parsed rather than grepped: prose naming boto3 is not a violation - the
    frozen seam in ``app/evidence.py`` states the rule it enforces - but an
    import is (F09 s16, **D**).
    """
    found: list[ast.stmt] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        if any(module.split(".")[0] in AWS_SDK_PACKAGES for module in modules):
            found.append(node)
    return found


def test_boto3_is_imported_in_exactly_one_module() -> None:
    """F09 s16 (D) - "an import elsewhere is a review failure and is greppable in CI"."""
    importers = sorted(
        path.relative_to(APP_DIR).as_posix() for path in APP_DIR.rglob("*.py") if sdk_import_nodes(path)
    )
    assert importers == ["reranker.py"], f"the AWS SDK may only be imported by the adapter; found {importers}"


def test_the_adapter_imports_the_sdk_lazily_not_at_module_scope() -> None:
    """A top-level import would make the whole app unimportable without boto3 installed."""
    nodes = sdk_import_nodes(APP_DIR / "reranker.py")
    assert nodes, "the adapter is expected to import the SDK somewhere"
    for node in nodes:
        assert node.col_offset > 0, f"module-scope AWS SDK import on line {node.lineno}"


def test_boto3_is_imported_only_when_bedrock_is_called() -> None:
    """boto3 is declared (2026-09-24, to run Cohere in production) but still loaded lazily:
    importing the app never imports it, so the offline gate needs no AWS SDK or credentials."""
    import subprocess
    import sys

    code = "import sys, app.main, app.reranker; print('boto3' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=APP_DIR.parent, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_the_bedrock_model_id_is_pinned_to_cohere_rerank_3_5() -> None:
    assert BEDROCK_RERANK_MODEL_ID == "cohere.rerank-v3-5:0"
    assert BedrockReranker(build_corpus(), client=StubBedrockClient()).model_id == BEDROCK_RERANK_MODEL_ID


def test_bedrock_request_carries_the_query_candidates_and_pinned_model(
    corpus: Corpus, candidates: tuple[Candidate, ...]
) -> None:
    client = StubBedrockClient()
    BedrockReranker(corpus, client=client).rerank(QUERY, candidates, top_k=3)

    assert len(client.calls) == 1
    request = client.calls[0]
    assert request["queries"] == [{"type": "TEXT", "textQuery": {"text": QUERY}}]
    assert len(request["sources"]) == len(candidates)
    configuration = request["rerankingConfiguration"]["bedrockRerankingConfiguration"]
    assert configuration["numberOfResults"] == 3
    assert configuration["modelConfiguration"]["modelArn"].endswith(BEDROCK_RERANK_MODEL_ID)


def test_a_malformed_bedrock_response_is_unavailable_not_a_crash(
    corpus: Corpus, candidates: tuple[Candidate, ...]
) -> None:
    """F09 s10 - "Adapter validates the response; malformed -> unavailable"."""
    client = StubBedrockClient(results=[{"index": 999, "relevanceScore": 0.5}])
    package = BedrockReranker(corpus, client=client).rerank(QUERY, candidates)
    assert package.status is RetrievalStatus.UNAVAILABLE
    assert package.degraded_reason == RERANKER_UNAVAILABLE_REASON


def test_fewer_results_than_requested_is_still_ok(corpus: Corpus, candidates: tuple[Candidate, ...]) -> None:
    """F09 s14 adversarial - Bedrock returning fewer results than asked for."""
    client = StubBedrockClient(results=[{"index": 0, "relevanceScore": 0.9}, {"index": 1, "relevanceScore": 0.8}])
    package = BedrockReranker(corpus, client=client).rerank(QUERY, candidates, top_k=5)
    assert package.status is RetrievalStatus.OK
    assert len(package.snippets) == 2


def test_missing_boto3_raises_a_clear_configuration_error(corpus: Corpus, monkeypatch) -> None:
    """boto3 is not a dependency, so constructing a real client must fail legibly."""
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *args: object, **kwargs: object):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    adapter = BedrockReranker(corpus)
    with pytest.raises(ProviderConfigurationError) as caught:
        adapter.client()
    assert "boto3" in str(caught.value)


def test_missing_boto3_still_degrades_rather_than_raising(
    corpus: Corpus, candidates: tuple[Candidate, ...], monkeypatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *args: object, **kwargs: object):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    package = BedrockReranker(corpus).rerank(QUERY, candidates)
    assert package.status is RetrievalStatus.UNAVAILABLE
    assert package.degraded_reason == RERANKER_UNAVAILABLE_REASON


def test_the_fake_makes_no_network_call(corpus: Corpus, candidates: tuple[Candidate, ...], monkeypatch) -> None:
    """F09 s13 AC-7 - CI never calls Bedrock."""
    import socket

    def forbid(*args: object, **kwargs: object):
        raise AssertionError("the offline gate must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)

    package = FakeReranker(corpus).rerank(QUERY, candidates)
    assert package.status is RetrievalStatus.OK


def test_a_bedrock_failure_logs_its_aws_error_code_but_never_its_message(
    corpus: Corpus, candidates: tuple[Candidate, ...], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A first live call that fails must say why (permissions? region?) without exporting the message."""
    from app.reranker import BedrockReranker

    class AccessDenied(Exception):
        response = {"Error": {"Code": "AccessDeniedException", "Message": "secret detail about arn:aws:iam::123"}}

    monkeypatch.setattr("app.reranker._sleep", lambda _s: None)
    reranker = BedrockReranker(corpus, client=StubBedrockClient(error=AccessDenied("secret detail about arn:aws:iam::123")), max_attempts=2)
    with caplog.at_level("INFO"):
        package = reranker.rerank(QUERY, candidates)

    assert package.status is RetrievalStatus.UNAVAILABLE
    logged = [r for r in caplog.records if r.getMessage() == "rerank.bedrock_error"]
    assert len(logged) == 2  # one per attempt
    blob = " ".join(json.dumps(r.__dict__, default=str) for r in logged)
    assert "AccessDeniedException" in blob and "AccessDenied" in blob
    assert "secret detail" not in blob


def test_the_bedrock_region_default_is_the_one_the_account_may_use() -> None:
    """The settings default and the adapter default agree, and both are us-east-1: the AWS
    organisation's region policy denies bedrock:Rerank in us-west-2 (verified 2026-09-25), so a
    drifted default would silently degrade every briefing to "reranker unavailable"."""
    from app.reranker import DEFAULT_BEDROCK_REGION
    from app.settings import ServiceSettings

    assert DEFAULT_BEDROCK_REGION == "us-east-1"
    assert ServiceSettings.model_fields["bedrock_region"].default == DEFAULT_BEDROCK_REGION
