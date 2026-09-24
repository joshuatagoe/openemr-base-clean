"""End-to-end briefing tests: POST /v1/briefings and BriefingService.

The model provider is always a scripted fake (see conftest.py). Each test
names the failure mode it guards against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.contracts import BriefingRequest, EvidenceSource, EvidenceState
from app.extractor import MODEL_NOTES_WARNING, REJECTED_SPAN_WARNING
from app.main import CORRELATION_HEADER
from app.providers.base import (
    MalformedModelOutputError,
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.service import NO_USABLE_PLAN_WARNING, PLAN_TOO_LONG_WARNING, BriefingService, select_plan_text
from tests.fakes import FakeProvider, hba1c, metformin, model_output

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "lab_followup.json"
CID = "7f0e6b2a-3c4d-4e5f-9a1b-2c3d4e5f6a7b"
PUUID = "3b9d2c1e-8f7a-4b6c-9d0e-1f2a3b4c5d6e"


def by_kind(body: dict) -> dict[str, dict]:
    return {m["commitment"]["kind"]: m for m in body["matches"]}


# --------------------------------------------------------------------------- #
# Complete scenario: HbA1c follow-up commitment + later final HbA1c result
# --------------------------------------------------------------------------- #


def test_full_scenario_matching_result_found(client: TestClient, fixture_payload: dict, fake_provider: FakeProvider) -> None:
    """Tracer bullet end to end: commitment, evidence state, neutral summary, and source citations."""
    resp = client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["correlation_id"] == CID and body["patient_uuid"] == PUUID
    assert resp.headers[CORRELATION_HEADER] == CID
    assert fake_provider.calls == ["Continue metformin. Repeat HbA1c in three months."]  # plan text only

    lab = by_kind(body)["lab_test"]
    assert lab["commitment"]["source_span"] == "Repeat HbA1c in three months."
    assert lab["commitment"]["test_name"] == "HbA1c"
    assert lab["commitment"]["due_text"] == "in three months"
    assert lab["state"] == EvidenceState.MATCHING_RESULT_FOUND.value
    assert "8.9%" in lab["summary"] and "high" in lab["summary"]
    for forbidden in ("should", "recommend", "increase", "adjust", "uncontrolled"):
        assert forbidden not in lab["summary"].lower()
    cited = {(c["record_type"], c["record_id"], c["timestamp"]) for c in lab["citations"]}
    assert cited == {
        ("prior_note", "form_soap:1001", "2026-06-10T14:30:00Z"),
        ("lab_result", "procedure_result:9001", "2026-09-12T09:15:00Z"),
    }
    assert body["warnings"] == []


def test_ids_are_result_scoped_in_source_order(client: TestClient, fixture_payload: dict) -> None:
    body = client.post("/v1/briefings", json=fixture_payload).json()
    assert [m["commitment"]["commitment_id"] for m in body["matches"]] == ["c-001", "c-002"]
    assert [m["commitment"]["kind"] for m in body["matches"]] == ["medication", "lab_test"]


# --------------------------------------------------------------------------- #
# Explicit cases
# --------------------------------------------------------------------------- #


def test_no_matching_result(client: TestClient, fixture_payload: dict) -> None:
    """Boundary: no subsequent result -> scoped absence claim citing only the note; never 'not done'."""
    fixture_payload["context"]["lab_results"] = []
    body = client.post("/v1/briefings", json=fixture_payload).json()
    lab = by_kind(body)["lab_test"]
    assert lab["state"] == EvidenceState.NO_MATCHING_RECORD_FOUND.value
    assert [c["record_type"] for c in lab["citations"]] == ["prior_note"]
    assert "supplied records" in lab["summary"]


def test_medication_commitment_with_no_records_is_scoped_absence(client: TestClient, fixture_payload: dict) -> None:
    """Boundary: the fixture carries no medication rows, so 'continue metformin' is no_matching_record_found - never 'not done'."""
    body = client.post("/v1/briefings", json=fixture_payload).json()
    med = by_kind(body)["medication"]
    assert med["commitment"]["source_span"] == "Continue metformin."
    assert med["state"] == EvidenceState.NO_MATCHING_RECORD_FOUND.value
    assert "either source" in med["summary"] and "not done" not in med["summary"]


def test_unavailable_lab_source_is_verification_unavailable(client: TestClient, fixture_payload: dict) -> None:
    """Guards: 'could not check' never collapses into 'nothing found' across the whole pipeline."""
    fixture_payload["context"]["data_quality"]["sources_unavailable"] = [EvidenceSource.LAB_RESULTS.value]
    body = client.post("/v1/briefings", json=fixture_payload).json()
    assert by_kind(body)["lab_test"]["state"] == EvidenceState.VERIFICATION_UNAVAILABLE.value


@pytest.mark.parametrize("plan_text", ["---", "...", "***"])
def test_no_usable_prior_plan_skips_the_model(client: TestClient, fixture_payload: dict, fake_provider: FakeProvider, plan_text: str) -> None:
    """Boundary: a plan with no letters or digits is reported as unusable and the model is never called."""
    fixture_payload["context"]["prior_note"]["plan_text"] = plan_text
    resp = client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 200
    assert resp.json()["matches"] == []
    assert resp.json()["warnings"] == [NO_USABLE_PLAN_WARNING]
    assert fake_provider.calls == []


@pytest.mark.parametrize(
    "error, status_code, code",
    [
        (ProviderUnavailableError("u"), 503, "provider_unavailable"),
        (ProviderRateLimitError("r"), 503, "provider_rate_limited"),
        (ProviderTimeoutError("t"), 504, "provider_timeout"),
        (ProviderAuthenticationError("a"), 503, "provider_authentication_failed"),
        (MalformedModelOutputError("m"), 502, "malformed_model_output"),
    ],
)
def test_provider_failure_is_an_explicit_error_not_an_empty_briefing(
    fixture_payload: dict, error: Exception, status_code: int, code: str
) -> None:
    """Guards: an outage or bad output is a structured error with identifiers, never a 200 with no matches."""
    from app.main import app, get_provider_factory

    provider = FakeProvider(error)
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/briefings", json=fixture_payload)
    finally:
        app.dependency_overrides.pop(get_provider_factory, None)

    assert resp.status_code == status_code
    detail = resp.json()["detail"]
    assert detail["code"] == code
    assert detail["correlation_id"] == CID and detail["patient_uuid"] == PUUID
    assert resp.headers[CORRELATION_HEADER] == CID
    assert "matches" not in resp.json()
    assert "metformin" not in resp.text and "HbA1c" not in resp.text  # no clinical content in error bodies
    expected_calls = 2 if getattr(error, "retryable", False) else 1  # bounded retry (PROVIDER_MAX_ATTEMPTS=2 in tests; 3 in production)
    assert len(provider.calls) == expected_calls


def test_rate_limit_error_carries_retry_after(fixture_payload: dict) -> None:
    from app.main import app, get_provider_factory

    app.dependency_overrides[get_provider_factory] = lambda: (lambda: FakeProvider(ProviderRateLimitError("r")))
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/briefings", json=fixture_payload)
    finally:
        app.dependency_overrides.pop(get_provider_factory, None)
    assert resp.status_code == 503 and resp.headers["Retry-After"] == "5"


def test_provider_not_configured_returns_503(unconfigured_client: TestClient, fixture_payload: dict) -> None:
    """Guards: with no API key the real provider cannot be built; the request fails explicitly and nothing is sent."""
    resp = unconfigured_client.post("/v1/briefings", json=fixture_payload)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "provider_not_configured"
    assert resp.json()["detail"]["correlation_id"] == CID


def test_health_does_not_require_provider(unconfigured_client: TestClient) -> None:
    assert unconfigured_client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("provider_script", [[model_output(hba1c(source_span="Repeat A1c in 3 months."))]])
def test_ungrounded_extraction_yields_no_matches_and_a_generic_warning(client: TestClient, fixture_payload: dict) -> None:
    """Guards: a hallucinated span produces no match and a fixed warning that carries no clinical text."""
    body = client.post("/v1/briefings", json=fixture_payload).json()
    assert body["matches"] == []
    assert body["warnings"] == [REJECTED_SPAN_WARNING]
    assert "A1c" not in " ".join(body["warnings"])


@pytest.mark.parametrize("provider_script", [[model_output(hba1c(), warnings=["Patient likely non-adherent"])]])
def test_model_authored_warnings_are_not_shown(client: TestClient, fixture_payload: dict) -> None:
    """Guards: model prose never reaches the user; only its count is reported."""
    body = client.post("/v1/briefings", json=fixture_payload).json()
    assert body["warnings"] == [MODEL_NOTES_WARNING.format(n=1)]
    assert "non-adherent" not in json.dumps(body)


# --------------------------------------------------------------------------- #
# Service-level
# --------------------------------------------------------------------------- #


def load_request() -> BriefingRequest:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return BriefingRequest.model_validate(json.load(fh))


def test_select_plan_text_rules() -> None:
    request = load_request()
    assert select_plan_text(request.context) == ("Continue metformin. Repeat HbA1c in three months.", [])
    long_ctx = request.context.model_copy(update={"prior_note": request.context.prior_note.model_copy(update={"plan_text": "x" * 21})}, deep=True)
    assert select_plan_text(long_ctx, max_chars=20) == (None, [PLAN_TOO_LONG_WARNING])
    blank_ctx = request.context.model_copy(update={"prior_note": request.context.prior_note.model_copy(update={"plan_text": "- - -"})}, deep=True)
    assert select_plan_text(blank_ctx) == (None, [NO_USABLE_PLAN_WARNING])


@pytest.mark.anyio
async def test_service_constructs_provider_lazily_per_briefing() -> None:
    """Guards: building the service never builds a provider; each briefing gets a fresh provider from the factory."""
    built: list[FakeProvider] = []

    def factory() -> FakeProvider:
        provider = FakeProvider(model_output(hba1c()))
        built.append(provider)
        return provider

    service = BriefingService(factory)
    assert built == []
    request = load_request()
    first = await service.build_briefing(request)
    second = await service.build_briefing(request)
    assert len(built) == 2
    assert first == second
    assert first.matches[0].state is EvidenceState.MATCHING_RESULT_FOUND


@pytest.mark.anyio
async def test_service_does_not_mutate_request() -> None:
    request = load_request()
    before = request.model_dump()
    await BriefingService(lambda: FakeProvider(model_output(hba1c()))).build_briefing(request)
    assert request.model_dump() == before


# =========================================================================== #
# Week 2 - grounded briefing with tiers and considerations (F11, ADR-006)
#
# Everything above this banner is Week 1's UC-01 pipeline and is untouched.
# The tests below exercise app.briefing.build_briefing, which consumes an
# EvidencePackage (app/evidence.py) and never reaches into the corpus or the
# retriever itself.
# =========================================================================== #

from datetime import UTC, date, datetime
from decimal import Decimal

from app.briefing import (
    COMPUTED_LABEL,
    EVIDENCE_UNAVAILABLE_LIMITATION,
    HEADINGS,
    NOTHING_TO_REPORT,
    PRINTED_FLAG_LABEL,
    AssertionTier,
    ChartFact,
    CitedFact,
    ConsiderationCandidate,
    DropReason,
    PatientReport,
    build_briefing,
    render_briefing,
    uncovered_topic_limitation,
)
from app.contracts import ADVICE_REFUSAL_TEXT
from app.contracts import Citation as RecordCitation
from app.contracts import RecordType
from app.corpus import ClaimKind, EvidenceTier
from app.documents import (
    AbnormalFlag,
    AbnormalFlagSource,
    DocumentCitation,
    ExtractionMetadata,
    LabDocument,
    LabResult,
    VerificationStatus,
)
from app.evidence import EvidencePackage, EvidenceSnippet, GuidelineCitation, RetrievalStatus

CORPUS_VERSION = "corpus-2026.09.23-abc123"

NDEP_QUOTE = (
    "In persons who have sufficiently long life expectancy to see microvascular benefits, "
    "an A1C goal of less than 7 percent is reasonable."
)
CDC_QUOTE = "Most people with diabetes have their A1C tested at least twice a year."


def _guideline_citation(
    *,
    chunk_id: str,
    quote: str,
    publisher: str,
    year: int | None,
    population_scope: str,
    tier: EvidenceTier,
    section: str,
) -> GuidelineCitation:
    return GuidelineCitation(
        source_id=chunk_id,
        page_or_section=section,
        field_or_chunk_id=chunk_id,
        quote_or_value=quote,
        corpus_version=CORPUS_VERSION,
        publisher=publisher,
        publication_year=year,
        population_scope=population_scope,
        evidence_tier=tier,
    )


def _tier_a_snippet() -> EvidenceSnippet:
    return EvidenceSnippet(
        chunk_id="ndep-p7-0001",
        text=NDEP_QUOTE,
        doc_title="Guiding Principles for the Care of People With or at Risk for Diabetes",
        publisher="National Diabetes Education Program",
        section="Principle 7 - Individualize glycemic goals",
        relevance_score=0.91,
        citation=_guideline_citation(
            chunk_id="ndep-p7-0001",
            quote=NDEP_QUOTE,
            publisher="National Diabetes Education Program",
            year=2018,
            population_scope="General US adults in primary care",
            tier=EvidenceTier.A,
            section="Principle 7 - Individualize glycemic goals",
        ),
    )


def _tier_b_snippet() -> EvidenceSnippet:
    return EvidenceSnippet(
        chunk_id="cdc-a1c-0001",
        text=CDC_QUOTE,
        doc_title="CDC A1C testing guidance",
        publisher="Centers for Disease Control and Prevention",
        section="A1C testing - how often",
        relevance_score=0.72,
        citation=_guideline_citation(
            chunk_id="cdc-a1c-0001",
            quote=CDC_QUOTE,
            publisher="Centers for Disease Control and Prevention",
            year=None,
            population_scope="General US adults",
            tier=EvidenceTier.B,
            section="A1C testing - how often",
        ),
    )


def _package(
    *snippets: EvidenceSnippet,
    status: RetrievalStatus = RetrievalStatus.OK,
    reason: str | None = None,
) -> EvidencePackage:
    return EvidencePackage(
        snippets=tuple(snippets),
        corpus_version=CORPUS_VERSION,
        reranker_model_id="fake-reranker-v1",
        status=status,
        degraded_reason=reason,
    )


def _unavailable_package() -> EvidencePackage:
    return EvidencePackage(
        snippets=(),
        corpus_version=CORPUS_VERSION,
        reranker_model_id="fake-reranker-v1",
        status=RetrievalStatus.UNAVAILABLE,
        degraded_reason="guideline_index_unavailable",
    )


def _lab_result(
    *,
    name: str = "Hemoglobin A1c",
    value: object = Decimal("8.9"),
    unit: str | None = "%",
    reference_range: str | None = "4.0-5.6",
    flag: AbnormalFlag | None = None,
    flag_source: AbnormalFlagSource = AbnormalFlagSource.UNAVAILABLE,
    status: VerificationStatus = VerificationStatus.VERIFIED_EXACT,
    quote: str = "8.9",
    index: int = 0,
) -> LabResult:
    return LabResult(
        test_name=name,
        value=value,
        unit=unit,
        reference_range=reference_range,
        collection_date=date(2026, 9, 12),
        abnormal_flag=flag,
        abnormal_flag_source=flag_source,
        verification_status=status,
        citation=DocumentCitation(
            source_id="55",
            page_or_section="p. 1",
            field_or_chunk_id=f"results[{index}].value",
            quote_or_value=quote,
        ),
    )


def _lab_document(*results: LabResult) -> LabDocument:
    return LabDocument(
        document_id=55,
        collection_date=date(2026, 9, 12),
        ordering_provider="Dr Alvarez",
        results=list(results),
        extraction_metadata=ExtractionMetadata(
            model_id="stub-deterministic",
            prompt_version="v1",
            extracted_at=datetime(2026, 9, 12, 10, 0, tzinfo=UTC),
            page_count=1,
            verified_fraction=1.0,
            unreadable_count=sum(1 for r in results if r.verification_status is VerificationStatus.UNREADABLE),
            unverified_count=0,
        ),
    )


def _prior_a1c() -> ChartFact:
    return ChartFact(
        test_name="Hemoglobin A1c",
        value="7.8",
        unit="%",
        observed_on=date(2026, 6, 10),
        citation=RecordCitation(
            record_type=RecordType.LAB_RESULT,
            record_id="procedure_result:9001",
            timestamp=datetime(2026, 6, 10, 14, 30, tzinfo=UTC),
        ),
    )


def _a1c_fact() -> CitedFact:
    return CitedFact(
        text="Hemoglobin A1c 8.9 % collected 2026-09-12",
        tier=AssertionTier.DOCUMENT_STATED,
        document_citation=_lab_result().citation,
        not_yet_in_chart=True,
    )


def _target_candidate() -> ConsiderationCandidate:
    """A threshold-kind consideration: it names a clinical goal."""
    return ConsiderationCandidate(
        consideration_id="c-target",
        topic="glycaemic target individualisation",
        claim_kind=ClaimKind.THRESHOLD,
        text=(
            "Guidance names an A1C goal of less than 7 percent for persons with long life "
            "expectancy; this patient's latest A1C is 8.9 %."
        ),
        relevance=(
            "This patient has no complications recorded in the supplied records, which is the "
            "group the cited passage conditions its goal on."
        ),
        facts=(_a1c_fact(),),
        supporting_chunk_ids=("ndep-p7-0001",),
        uncertainty="Life expectancy and complication history are not in the supplied records.",
    )


def _interval_candidate() -> ConsiderationCandidate:
    """A care-process consideration: how often a test is typically done."""
    return ConsiderationCandidate(
        consideration_id="c-interval",
        topic="A1C testing interval",
        claim_kind=ClaimKind.CARE_PROCESS,
        text="Guidance describes A1C testing at least twice a year; this patient's previous A1C was 2026-06-10.",
        relevance="Only one prior A1C is in the supplied records for this patient, dated 2026-06-10.",
        facts=(
            CitedFact(
                text="Prior Hemoglobin A1c 7.8 % on 2026-06-10",
                tier=AssertionTier.CHART_FACT,
                record_citation=_prior_a1c().citation,
            ),
        ),
        supporting_chunk_ids=("cdc-a1c-0001",),
        uncertainty=None,
    )


def _full_briefing():
    return build_briefing(
        document=_lab_document(_lab_result()),
        evidence=_package(_tier_a_snippet(), _tier_b_snippet()),
        prior_facts=(_prior_a1c(),),
        considerations=(_target_candidate(), _interval_candidate()),
    )


# --------------------------------------------------------------------------- #
# F11 AC-1: three headings, and every clinical claim carries the right citation
# --------------------------------------------------------------------------- #


def test_three_headings_render_with_a_citation_of_the_correct_kind_on_every_claim() -> None:
    briefing = _full_briefing()

    assert [section.heading for section in briefing.sections()] == list(HEADINGS)
    rendered = render_briefing(briefing)
    for heading in HEADINGS:
        assert heading in rendered

    assert briefing.what_changed, "a new value against a differing prior chart value belongs under What changed"
    assert briefing.needs_attention, "a value above its printed range belongs under Needs attention"
    assert briefing.what_to_consider, "both supported considerations belong under What to consider"

    for line in (*briefing.what_changed, *briefing.needs_attention):
        if line.tier in (AssertionTier.DOCUMENT_STATED, AssertionTier.COMPUTED, AssertionTier.PATIENT_REPORTED):
            assert line.document_citation is not None
        elif line.tier is AssertionTier.CHART_FACT:
            assert line.record_citation is not None
        else:  # pragma: no cover - a guideline claim never sits under these headings
            raise AssertionError(f"unexpected tier under a record heading: {line.tier}")

    for consideration in briefing.what_to_consider:
        assert consideration.tier is AssertionTier.GUIDELINE_SUPPORTED
        assert consideration.citations, "a consideration without guideline support must not be displayed"
        assert consideration.facts, "a consideration must name the patient fact it rests on"
        assert consideration.relevance, "patient-specific relevance is mandatory (F11 3.3)"


# --------------------------------------------------------------------------- #
# F11 AC-2 / W2-AMB-055: a computed comparison is never a lab-printed flag
# --------------------------------------------------------------------------- #


def test_value_above_printed_range_with_no_printed_flag_is_computed_and_shows_its_inputs() -> None:
    briefing = build_briefing(
        document=_lab_document(_lab_result(flag=None, flag_source=AbnormalFlagSource.UNAVAILABLE)),
        evidence=_unavailable_package(),
    )

    attention = [line for line in briefing.needs_attention if "A1c" in line.text]
    assert len(attention) == 1
    line = attention[0]

    assert line.tier is AssertionTier.COMPUTED
    assert line.abnormal_flag_source is AbnormalFlagSource.DERIVED
    assert line.computed is not None
    assert line.computed.value == "8.9"
    assert line.computed.reference_range == "4.0-5.6"
    assert line.computed.direction == "above"
    assert line.computed.rule

    rendered = render_briefing(briefing)
    assert COMPUTED_LABEL in rendered
    assert PRINTED_FLAG_LABEL not in rendered


def test_a_printed_flag_stays_document_stated_and_carries_no_computed_block() -> None:
    briefing = build_briefing(
        document=_lab_document(_lab_result(flag=AbnormalFlag.HIGH, flag_source=AbnormalFlagSource.EXTRACTED)),
        evidence=_unavailable_package(),
    )
    line = [ln for ln in briefing.needs_attention if "A1c" in ln.text][0]
    assert line.tier is AssertionTier.DOCUMENT_STATED
    assert line.computed is None
    rendered = render_briefing(briefing)
    assert PRINTED_FLAG_LABEL in rendered
    assert COMPUTED_LABEL not in rendered


# --------------------------------------------------------------------------- #
# ADR-006 4 / F07 3.7: tier admissibility
# --------------------------------------------------------------------------- #


def test_threshold_claim_supported_only_by_tier_b_is_absent_and_the_drop_is_counted() -> None:
    threshold_on_tier_b = _target_candidate().model_copy(update={"supporting_chunk_ids": ("cdc-a1c-0001",)})
    briefing = build_briefing(
        document=None,
        evidence=_package(_tier_b_snippet()),
        considerations=(threshold_on_tier_b,),
    )

    assert briefing.what_to_consider == ()
    assert briefing.dropped_count == 1
    dropped = briefing.dropped[0]
    assert dropped.claim_id == "c-target"
    assert dropped.reason is DropReason.TIER_INADMISSIBLE
    assert "7 percent" not in render_briefing(briefing)


def test_tier_b_snippet_supports_a_care_process_consideration_with_its_tier_visible() -> None:
    briefing = build_briefing(
        document=None,
        evidence=_package(_tier_b_snippet()),
        considerations=(_interval_candidate(),),
    )

    assert briefing.dropped_count == 0
    assert len(briefing.what_to_consider) == 1
    consideration = briefing.what_to_consider[0]
    assert consideration.claim_kind is ClaimKind.CARE_PROCESS
    assert consideration.citations[0].evidence_tier is EvidenceTier.B
    assert EvidenceTier.B.label in render_briefing(briefing)


def test_every_guideline_citation_exposes_population_scope_and_evidence_tier() -> None:
    briefing = _full_briefing()
    rendered = render_briefing(briefing)

    seen = 0
    for consideration in briefing.what_to_consider:
        for citation in consideration.citations:
            seen += 1
            assert citation.population_scope
            assert citation.publisher
            assert citation.population_scope in rendered
            assert citation.publisher in rendered
            assert citation.evidence_tier.label in rendered
            if citation.publication_year is not None:
                assert str(citation.publication_year) in rendered
    assert seen == 2


# --------------------------------------------------------------------------- #
# F11 4: failure behaviour
# --------------------------------------------------------------------------- #


def test_dosing_request_returns_the_fixed_refusal_and_no_dosing_guidance() -> None:
    briefing = build_briefing(
        document=_lab_document(_lab_result()),
        evidence=_package(_tier_a_snippet(), _tier_b_snippet()),
        prior_facts=(_prior_a1c(),),
        considerations=(_target_candidate(), _interval_candidate()),
        question="Should I increase her metformin dose?",
    )

    assert briefing.refusal == ADVICE_REFUSAL_TEXT
    rendered = render_briefing(briefing)
    assert ADVICE_REFUSAL_TEXT in rendered
    for banned in ("increase", "dose", "dosage", "titrate", "should", "recommend", " mg"):
        assert banned not in rendered.lower(), f"{banned!r} leaked into a refused briefing"


def test_a_directive_statement_is_dropped_before_display_and_counted() -> None:
    directive = _target_candidate().model_copy(
        update={"text": "This patient should have the metformin dose increased to reach the cited goal."}
    )
    briefing = build_briefing(document=None, evidence=_package(_tier_a_snippet()), considerations=(directive,))
    assert briefing.what_to_consider == ()
    assert briefing.dropped[0].reason is DropReason.DIRECTIVE_LANGUAGE
    assert "metformin" not in render_briefing(briefing)


def test_unavailable_evidence_yields_zero_guideline_claims_and_states_the_limitation() -> None:
    briefing = build_briefing(
        document=_lab_document(_lab_result()),
        evidence=_unavailable_package(),
        prior_facts=(_prior_a1c(),),
        considerations=(_target_candidate(), _interval_candidate()),
    )

    assert briefing.what_to_consider == ()
    assert EVIDENCE_UNAVAILABLE_LIMITATION in briefing.limitations
    assert EVIDENCE_UNAVAILABLE_LIMITATION in render_briefing(briefing)
    # The record-derived headings are unaffected: a retrieval outage is not a chart outage.
    assert briefing.what_changed and briefing.needs_attention


def test_uncovered_topic_is_named_as_a_limitation_and_every_supported_consideration_still_shows() -> None:
    uncovered = ConsiderationCandidate(
        consideration_id="c-uncovered",
        topic="sick-day insulin adjustment",
        claim_kind=ClaimKind.CARE_PROCESS,
        text="No passage in the selected corpus addresses this topic.",
        relevance="The patient reported two days of vomiting at intake.",
        facts=(_a1c_fact(),),
        supporting_chunk_ids=(),
        uncertainty=None,
    )
    briefing = build_briefing(
        document=None,
        evidence=_package(_tier_a_snippet(), _tier_b_snippet()),
        considerations=(_target_candidate(), uncovered, _interval_candidate()),
    )

    assert [c.consideration_id for c in briefing.what_to_consider] == ["c-target", "c-interval"]
    assert uncovered_topic_limitation("sick-day insulin adjustment") in briefing.limitations
    assert briefing.dropped_count == 1
    assert briefing.dropped[0].reason is DropReason.UNRESOLVABLE_CITATION


def test_a_claim_whose_citation_does_not_resolve_is_dropped_and_counted() -> None:
    unresolvable = _target_candidate().model_copy(update={"supporting_chunk_ids": ("ndep-p7-9999",)})
    briefing = build_briefing(document=None, evidence=_package(_tier_a_snippet()), considerations=(unresolvable,))

    assert briefing.what_to_consider == ()
    assert briefing.dropped_count == 1
    assert briefing.dropped[0].reason is DropReason.UNRESOLVABLE_CITATION
    # A dangling chunk id is a system fault, not a gap in the corpus.
    assert briefing.limitations == ()


def test_a_number_absent_from_the_cited_source_drops_the_statement() -> None:
    invented = _target_candidate().model_copy(
        update={"text": "Guidance names an A1C goal of less than 6.5 percent; this patient's latest A1C is 8.9 %."}
    )
    briefing = build_briefing(document=None, evidence=_package(_tier_a_snippet()), considerations=(invented,))

    assert briefing.what_to_consider == ()
    assert briefing.dropped[0].reason is DropReason.NUMERIC_UNSUPPORTED
    assert "6.5" not in render_briefing(briefing)


def test_nothing_to_report_says_so_rather_than_manufacturing_content() -> None:
    briefing = build_briefing(document=None, evidence=_unavailable_package())

    assert briefing.what_changed == () and briefing.needs_attention == () and briefing.what_to_consider == ()
    assert briefing.nothing_to_report is True
    assert NOTHING_TO_REPORT in render_briefing(briefing)


# --------------------------------------------------------------------------- #
# F11 3.5: a patient report is an attributed observation, never a finding
# --------------------------------------------------------------------------- #


def test_patient_reported_conflict_is_attributed_and_leaves_the_chart_alone() -> None:
    report = PatientReport(
        text="The intake form reports the patient stopped metformin; the chart lists it as active.",
        document_citation=DocumentCitation(
            source_id="61",
            page_or_section="p. 1",
            field_or_chunk_id="medications[0]",
            quote_or_value="stopped metformin",
        ),
        chart_citation=RecordCitation(
            record_type=RecordType.MEDICATION,
            record_id="prescriptions:4412",
            timestamp=datetime(2026, 6, 10, 14, 30, tzinfo=UTC),
        ),
        native_link="/interface/patient_file/summary/demographics.php?set_pid=7",
    )
    briefing = build_briefing(document=None, evidence=_unavailable_package(), patient_reports=(report,))

    line = briefing.needs_attention[0]
    assert line.tier is AssertionTier.PATIENT_REPORTED
    assert line.document_citation is not None and line.record_citation is not None
    rendered = render_briefing(briefing)
    assert "patient-reported" in rendered
    assert report.native_link in rendered
