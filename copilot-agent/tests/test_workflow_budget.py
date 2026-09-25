"""The document briefing's wall-clock budget and iteration cap (W2 build plan, Lane 3).

Both end the run with ``status="degraded"`` and a fixed reason - never an
exception, never a hung request - and keep whatever was already produced:
the routing log, and the record-derived lines when the document was read.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.document_briefing import BriefingStatus, ConsiderationDraftSet, DocumentBriefingRequest, build_reranker
from app.lab_extractor import extract_lab_document
from app.providers.stub_provider import StubProvider
from app.workflow import BUDGET_EXHAUSTED, ITERATION_LIMIT, decide, run_supervised_briefing

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"
PATIENT = "a2c3ab57-cdd6-4aad-afc9-e19c171e7ed7"


class _SlowAnswer(StubProvider):
    """Extraction is instant; the answer model takes longer than any test budget."""

    async def parse_structured(self, *, schema: Any, **kwargs: Any) -> Any:
        if schema is ConsiderationDraftSet:
            await asyncio.sleep(5)
        return await super().parse_structured(schema=schema, **kwargs)


class _SlowExtraction(StubProvider):
    async def parse_structured(self, *, schema: Any, **kwargs: Any) -> Any:
        if schema is not ConsiderationDraftSet:
            await asyncio.sleep(5)
        return await super().parse_structured(schema=schema, **kwargs)


def _legacy() -> DocumentBriefingRequest:
    return DocumentBriefingRequest(
        correlation_id=uuid.uuid4(),
        patient_uuid=uuid.UUID(PATIENT),
        document_id=101,
        media_type="application/pdf",
        document_base64=base64.b64encode((FIXTURES / "lab_hba1c_clean.pdf").read_bytes()).decode(),
    )


async def _stored() -> DocumentBriefingRequest:
    document = await extract_lab_document(
        document_id=201, pdf_bytes=(FIXTURES / "lab_hba1c_clean.pdf").read_bytes(), media_type="application/pdf", provider=StubProvider()
    )
    return DocumentBriefingRequest.model_validate(
        {
            "correlation_id": str(uuid.uuid4()),
            "patient_uuid": PATIENT,
            "documents": [{"document_id": 201, "doc_type": "lab_pdf", "extraction": document.model_dump(mode="json")}],
        }
    )


async def _run(request: DocumentBriefingRequest, provider: Any, **kwargs: Any) -> Any:
    return await run_supervised_briefing(request, provider=provider, reranker=build_reranker("fake", region="us-east-1"), **kwargs)


# --------------------------------------------------------------------------- #
# The routing rule
# --------------------------------------------------------------------------- #


def test_the_supervisor_stops_at_the_step_cap() -> None:
    state = {"document": object(), "routing": [object(), object()]}
    assert decide(state, max_steps=2) == ("finish", ITERATION_LIMIT)  # type: ignore[arg-type]
    assert decide(state, max_steps=3) == ("evidence-retriever", "evidence_required")  # type: ignore[arg-type]


def test_the_supervisor_stops_once_the_budget_is_spent() -> None:
    assert decide({"document": object()}, over_budget=True) == ("finish", BUDGET_EXHAUSTED)  # type: ignore[arg-type]


def test_a_finished_briefing_is_complete_even_at_the_cap() -> None:
    state = {"document": object(), "evidence": object(), "briefed": True, "routing": [object()] * 5}
    assert decide(state, max_steps=3, over_budget=True) == ("finish", "briefing_complete")  # type: ignore[arg-type]


def test_a_worker_failure_is_still_reported_as_the_worker_failure() -> None:
    state = {"status": BriefingStatus.DEGRADED, "routing": [object()] * 5}
    assert decide(state, max_steps=1, over_budget=True) == ("finish", "worker_failed")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Wall-clock budget
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_a_slow_answer_model_degrades_on_budget_and_keeps_the_record_lines() -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    response = await _run(await _stored(), _SlowAnswer(), budget_seconds=0.5)
    assert loop.time() - started < 3, "the budget must cut the run off, not wait for the model"
    assert response.status is BriefingStatus.DEGRADED
    assert response.degraded_reason == BUDGET_EXHAUSTED
    assert [d.target for d in response.routing] == ["evidence-retriever", "answer"]
    assert response.briefing is not None
    assert response.briefing.what_changed and not response.briefing.what_to_consider


@pytest.mark.anyio
async def test_a_slow_extraction_degrades_on_budget_with_no_briefing() -> None:
    response = await _run(_legacy(), _SlowExtraction(), budget_seconds=0.5)
    assert response.status is BriefingStatus.DEGRADED
    assert response.degraded_reason == BUDGET_EXHAUSTED
    assert [d.target for d in response.routing] == ["intake-extractor"]
    assert response.briefing is None


@pytest.mark.anyio
async def test_a_budget_spent_between_workers_stops_before_the_next_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The supervisor checks the clock before handing off, so no new model call starts late."""
    import app.workflow as wf

    clock = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(wf, "_now", lambda: next(clock, 100.0))
    response = await _run(await _stored(), StubProvider(), budget_seconds=10)
    assert [(d.target, d.reason_code) for d in response.routing] == [
        ("evidence-retriever", "evidence_required"),
        ("finish", BUDGET_EXHAUSTED),
    ]
    assert response.status is BriefingStatus.DEGRADED and response.degraded_reason == BUDGET_EXHAUSTED
    assert response.briefing is not None and not response.briefing.what_to_consider


# --------------------------------------------------------------------------- #
# Iteration cap
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_the_step_cap_ends_the_run_degraded_with_the_record_lines() -> None:
    response = await _run(await _stored(), StubProvider(), max_steps=1)
    assert [(d.step, d.target, d.reason_code) for d in response.routing] == [
        (1, "evidence-retriever", "evidence_required"),
        (2, "finish", ITERATION_LIMIT),
    ]
    assert response.status is BriefingStatus.DEGRADED and response.degraded_reason == ITERATION_LIMIT
    assert response.briefing is not None and response.briefing.what_changed


@pytest.mark.anyio
async def test_the_default_cap_leaves_a_normal_briefing_untouched() -> None:
    response = await _run(_legacy(), StubProvider())
    assert response.status is BriefingStatus.OK
    assert response.routing[-1].reason_code == "briefing_complete"


@pytest.mark.anyio
async def test_the_graph_recursion_backstop_degrades_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.errors import GraphRecursionError

    import app.workflow as wf

    class _Runaway:
        async def astream(self, *args: Any, **kwargs: Any) -> Any:
            raise GraphRecursionError("recursion limit")
            yield  # pragma: no cover

    monkeypatch.setattr(wf, "build_graph", lambda: _Runaway())
    response = await _run(_legacy(), StubProvider())
    assert response.status is BriefingStatus.DEGRADED and response.degraded_reason == ITERATION_LIMIT
    assert response.briefing is None


@pytest.mark.anyio
async def test_an_unexpected_error_in_the_graph_degrades_with_a_fixed_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.workflow as wf

    class _Broken:
        async def astream(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Hemoglobin A1c 8.9 %")  # clinical text must never reach the response
            yield  # pragma: no cover

    monkeypatch.setattr(wf, "build_graph", lambda: _Broken())
    response = await _run(_legacy(), StubProvider())
    assert response.status is BriefingStatus.DEGRADED and response.degraded_reason == "internal_error"
    assert "8.9" not in json.dumps(response.model_dump(mode="json"))


# --------------------------------------------------------------------------- #
# Over HTTP: the default budget sits under the module's client timeout
# --------------------------------------------------------------------------- #


def test_the_default_budget_is_below_the_module_timeout() -> None:
    """GuzzleAgentClient::DOCUMENT_BRIEFING_TIMEOUT_SECONDS is 90 s; the agent must answer first."""
    from app.workflow import DOCUMENT_BRIEFING_BUDGET_SECONDS

    assert 0 < DOCUMENT_BRIEFING_BUDGET_SECONDS <= 80
