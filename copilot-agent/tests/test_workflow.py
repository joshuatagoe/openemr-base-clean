"""Supervisor + two workers (PRD CR4, ADR-001) - app/workflow.py.

The routing rule is tested as a pure function, then the compiled graph end to
end: the path it takes, the handoff log it returns, and how each worker's
failure ends the run. Offline: StubProvider and FakeReranker.
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.document_briefing import (
    BriefingStatus,
    ConsiderationDraftSet,
    DocumentBriefingRequest,
    build_reranker,
)
from app.providers.base import ProviderUnavailableError
from app.providers.stub_provider import StubProvider
from app.workflow import (
    LANGSMITH_SWITCHES,
    LangSmithEnabledError,
    build_graph,
    decide,
    run_supervised_briefing,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"


def _request(pdf: str = "lab_hba1c_clean.pdf", **over: Any) -> DocumentBriefingRequest:
    values: dict[str, Any] = {
        "correlation_id": uuid.uuid4(),
        "patient_uuid": uuid.uuid4(),
        "document_id": 101,
        "media_type": "application/pdf",
        "document_base64": base64.b64encode((FIXTURES / pdf).read_bytes()).decode(),
    }
    values.update(over)
    return DocumentBriefingRequest(**values)


async def _run(request: DocumentBriefingRequest, provider: Any = None) -> Any:
    return await run_supervised_briefing(
        request, provider=provider or StubProvider(), reranker=build_reranker("fake", region="us-east-1")
    )


class _AnswerModelDown(StubProvider):
    """Extraction works; the answer model is unavailable."""

    async def parse_structured(self, *, schema: Any, **kwargs: Any) -> Any:
        if schema is ConsiderationDraftSet:
            raise ProviderUnavailableError("answer model down")
        return await super().parse_structured(schema=schema, **kwargs)


# --------------------------------------------------------------------------- #
# The routing rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, ("intake-extractor", "document_pending_extraction")),
        ({"document": object()}, ("evidence-retriever", "evidence_required")),
        ({"document": object(), "evidence": object()}, ("answer", "evidence_ready")),
        ({"document": object(), "evidence": object(), "briefed": True}, ("finish", "briefing_complete")),
        ({"status": BriefingStatus.DEGRADED}, ("finish", "worker_failed")),
        # A failure after extraction still stops the run - nothing is retried blindly.
        ({"document": object(), "status": BriefingStatus.DEGRADED}, ("finish", "worker_failed")),
    ],
)
def test_the_supervisor_routes_on_what_the_state_still_needs(state: dict, expected: tuple[str, str]) -> None:
    assert decide(state) == expected  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The compiled graph
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_a_briefing_visits_both_workers_then_the_answer_and_logs_every_handoff() -> None:
    response = await _run(_request())
    assert response.status is BriefingStatus.OK
    assert [(d.step, d.target, d.reason_code) for d in response.routing] == [
        (1, "intake-extractor", "document_pending_extraction"),
        (2, "evidence-retriever", "evidence_required"),
        (3, "answer", "evidence_ready"),
        (4, "finish", "briefing_complete"),
    ]
    assert {d.doc_type for d in response.routing} == {"lab_pdf"}
    assert response.briefing is not None and response.briefing.what_to_consider


@pytest.mark.anyio
async def test_an_extraction_failure_ends_the_run_without_calling_the_retriever() -> None:
    response = await _run(_request(document_base64="!!!not base64!!!"))
    assert response.status is BriefingStatus.DEGRADED
    assert response.degraded_reason == "document_not_decodable"
    assert [(d.target, d.reason_code) for d in response.routing] == [
        ("intake-extractor", "document_pending_extraction"),
        ("finish", "worker_failed"),
    ]
    assert response.briefing is None


@pytest.mark.anyio
async def test_an_answer_model_failure_still_returns_the_record_lines() -> None:
    response = await _run(_request(), provider=_AnswerModelDown())
    assert response.status is BriefingStatus.DEGRADED
    assert response.degraded_reason == "answer_model_unavailable"
    assert response.briefing is not None
    assert response.briefing.what_changed and not response.briefing.what_to_consider
    assert response.routing[-1].reason_code == "worker_failed"


def test_the_graph_has_no_checkpointer_so_document_state_is_never_persisted() -> None:
    assert build_graph().checkpointer is None


@pytest.mark.anyio
@pytest.mark.parametrize("switch", LANGSMITH_SWITCHES)
async def test_the_graph_refuses_to_run_with_langsmith_tracing_on(switch: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(switch, "true")
    with pytest.raises(LangSmithEnabledError):
        await _run(_request())
