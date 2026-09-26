"""Every pdfium call goes through one lock: pdfium is not thread-safe (2026-09-25 review).

Pages are rendered in worker threads while other requests count pages; without the lock two
documents processed at once can call into pdfium concurrently and crash the agent process.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.page_text as page_text


class _ConcurrencyProbe:
    """Stands in for pypdfium2.PdfDocument and records how many calls overlap."""

    active = 0
    peak = 0
    guard = threading.Lock()

    def __init__(self, *_: object) -> None:
        with _ConcurrencyProbe.guard:
            _ConcurrencyProbe.active += 1
            _ConcurrencyProbe.peak = max(_ConcurrencyProbe.peak, _ConcurrencyProbe.active)
        time.sleep(0.02)

    def __len__(self) -> int:
        return 1

    def close(self) -> None:
        with _ConcurrencyProbe.guard:
            _ConcurrencyProbe.active -= 1


def test_page_counts_from_many_threads_never_enter_pdfium_together(monkeypatch: pytest.MonkeyPatch) -> None:
    _ConcurrencyProbe.active = _ConcurrencyProbe.peak = 0
    monkeypatch.setattr(page_text.pdfium, "PdfDocument", _ConcurrencyProbe)
    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(lambda _: page_text.pdf_page_count(b"%PDF"), range(16)))
    assert counts == [1] * 16
    assert _ConcurrencyProbe.peak == 1, "pdfium was entered from two threads at once"


def test_every_pdfium_entry_point_uses_the_lock() -> None:
    import inspect

    for fn in (page_text.render_page_png, page_text.warm_renderer, page_text.pdf_page_count):
        assert "PDFIUM_LOCK" in inspect.getsource(fn), fn.__name__
