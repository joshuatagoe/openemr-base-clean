"""Words with boxes, from the page itself (ADR-007, contract C1).

Every source - the PDF text layer, OCR of a rendered page, OCR of a photo -
produces the same thing: a list of :class:`Word` in ONE coordinate frame.

    0-1 normalised, top-left origin, 1-based pages, relative to the page's
    CROPBOX with its /Rotate applied (PDF), or to the EXIF-oriented image
    (photos).

That is the frame a viewer draws: pdf.js and pypdfium2 both display the cropbox
rotated, and a browser shows a photo upright. OCR runs on pages we render
ourselves from the same cropbox, so a text-layer box and an OCR box on the same
page are directly comparable.

Words outside the cropbox are dropped, never clamped: a clamped box would sit
on the page edge over whatever is printed there.

PHI: nothing in this module logs a word, a page image or a file name. Logs carry
counts and fixed codes only.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Literal, Protocol

import pdfplumber

from app.observability import log_event

# pdfminer's DEBUG output includes content-stream tokens - document text. It must
# never reach a handler, even when the root logger is at DEBUG.
logging.getLogger("pdfminer").setLevel(logging.WARNING)
logging.getLogger("pdfplumber").setLevel(logging.WARNING)

WordSource = Literal["text_layer", "ocr"]


@dataclass(frozen=True)
class Word:
    text: str
    page: int  # 1-based
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1; 0-1; top-left origin
    source: WordSource


@dataclass(frozen=True)
class PageWords:
    page: int
    has_text_layer: bool
    has_images: bool
    words: tuple[Word, ...]


class OcrSource(Protocol):
    """Reads one upright page image. TextractOcr in production, FakeOcr in CI."""

    async def read_page(self, png: bytes, *, page: int) -> tuple[Word, ...]: ...


def normalised_box(
    x0: float, y0: float, x1: float, y1: float, *, width: float, height: float
) -> tuple[float, float, float, float] | None:
    """A box in page units as 0-1 fractions, or None when it is not wholly on the page."""
    if width <= 0 or height <= 0:
        return None
    box = (x0 / width, y0 / height, x1 / width, y1 / height)
    if not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
        return None
    return box


def _page_words(page: pdfplumber.page.Page, number: int) -> PageWords:
    # pdfplumber reports words top-left in the displayed (rotation-applied)
    # frame, in the same space as page.cropbox (verified on ink: plain,
    # cropbox-offset, /Rotate 90 and crop+rotate).
    cx0, ctop, cx1, cbottom = page.cropbox
    width, height = cx1 - cx0, cbottom - ctop
    words: list[Word] = []
    for raw in page.extract_words(keep_blank_chars=False, use_text_flow=False):
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        box = normalised_box(
            raw["x0"] - cx0, raw["top"] - ctop, raw["x1"] - cx0, raw["bottom"] - ctop,
            width=width, height=height,
        )
        if box is None:  # outside the cropbox: dropped, never clamped
            continue
        words.append(Word(text=text, page=number, bbox=box, source="text_layer"))
    try:
        has_images = bool(page.images)
    except Exception:  # noqa: BLE001 - a malformed image dictionary means "cannot tell": OCR fallback stays possible
        has_images = True
    return PageWords(page=number, has_text_layer=bool(words), has_images=has_images, words=tuple(words))


def text_layer_pages(pdf_bytes: bytes) -> tuple[PageWords, ...]:
    """Every page's text-layer words. An unparseable PDF yields no pages, never an exception."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return tuple(_page_words(page, number) for number, page in enumerate(pdf.pages, start=1))
    except Exception as exc:  # noqa: BLE001 - verification degrades; extraction never fails on it
        log_event("page_text.pdf_unreadable", error_type=type(exc).__name__)
        return ()


__all__ = [
    "OcrSource",
    "PageWords",
    "Word",
    "WordSource",
    "normalised_box",
    "text_layer_pages",
]
