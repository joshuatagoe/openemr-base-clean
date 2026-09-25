"""The pdfium renderer is warmed in the background at startup only when real OCR is on (ADR-007)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.page_text as page_text


async def _finish(task) -> None:  # noqa: ANN001 - an asyncio.Task from the app's own loop
    await task


@pytest.mark.parametrize(("ocr", "expected_calls"), [("textract", 1), ("fake", 0)])
def test_renderer_warms_only_with_real_ocr(ocr: str, expected_calls: int, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(page_text, "warm_renderer", lambda: calls.append(1))
    monkeypatch.setattr(main_module.settings, "ocr", ocr)
    with TestClient(main_module.app) as client:
        task = client.app.state.renderer_warmup
        if task is not None:
            client.portal.call(_finish, task)  # let the background warm-up finish inside the app's loop
    assert len(calls) == expected_calls


def test_a_failed_warmup_never_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise RuntimeError("pdfium unavailable")

    monkeypatch.setattr(page_text, "warm_renderer", boom)
    monkeypatch.setattr(main_module.settings, "ocr", "textract")
    with TestClient(main_module.app) as client:
        client.portal.call(_finish, client.app.state.renderer_warmup)
        assert client.get("/health").status_code == 200
