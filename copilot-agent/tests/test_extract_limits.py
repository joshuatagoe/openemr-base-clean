"""The extract step is bounded: a page cap and a time budget below the module's 90 s timeout.

Found in the 2026-09-25 architecture review: with OCR, each image page can cost a Textract call,
so an unbounded read could outlast the module's client timeout - the module gives up while the
agent keeps working and spending.
"""

from __future__ import annotations

import asyncio
import base64
import io
from typing import Any

import pypdfium2 as pdfium
import pytest

import app.document_briefing as db
from app.providers.stub_provider import StubProvider


def _pdf(pages: int) -> str:
    document = pdfium.PdfDocument.new()
    try:
        for _ in range(pages):
            document.new_page(612, 792)
        buf = io.BytesIO()
        document.save(buf)
    finally:
        document.close()
    return base64.b64encode(buf.getvalue()).decode()


@pytest.mark.anyio
async def test_a_document_over_the_page_cap_is_refused_before_any_model_or_ocr_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    async def never(**_: Any) -> Any:
        called.append(True)
        raise AssertionError("the extractor must not run")

    monkeypatch.setattr(db, "extract_lab_document", never)
    document, reason = await db.read_lab_document(
        document_id=1, document_base64=_pdf(db.MAX_DOCUMENT_PAGES + 1), media_type="application/pdf", provider=StubProvider()
    )
    assert (document, reason) == (None, "too_many_pages")
    assert called == []


@pytest.mark.anyio
async def test_a_document_at_the_page_cap_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(**_: Any) -> str:
        return "document"

    monkeypatch.setattr(db, "extract_lab_document", fake)
    document, reason = await db.read_lab_document(
        document_id=1, document_base64=_pdf(db.MAX_DOCUMENT_PAGES), media_type="application/pdf", provider=StubProvider()
    )
    assert (document, reason) == ("document", None)


@pytest.mark.anyio
async def test_a_read_past_the_budget_stops_with_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(**_: Any) -> str:
        await asyncio.sleep(5)
        return "too late"

    monkeypatch.setattr(db, "extract_lab_document", slow)
    document, reason = await db.read_lab_document(
        document_id=1, document_base64=_pdf(1), media_type="application/pdf", provider=StubProvider(), budget_seconds=0.05
    )
    assert (document, reason) == (None, "budget_exhausted")


def test_the_budget_is_below_the_module_timeout() -> None:
    assert db.DOCUMENT_EXTRACT_BUDGET_SECONDS < 90.0
