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

import asyncio
import io
import logging
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import pdfplumber
import threading

import pypdfium2 as pdfium

#: pdfium (under pypdfium2) is not thread-safe, and pages are rendered from worker threads
#: (asyncio.to_thread) while other requests may be counting pages. Every pdfium call in the
#: agent goes through this one lock. Found in the 2026-09-25 architecture review.
PDFIUM_LOCK = threading.Lock()
from PIL import Image, ImageOps, UnidentifiedImageError

from app.observability import log_event

# pdfminer's DEBUG output includes content-stream tokens - document text. It must
# never reach a handler, even when the root logger is at DEBUG.
logging.getLogger("pdfminer").setLevel(logging.WARNING)
logging.getLogger("pdfplumber").setLevel(logging.WARNING)
logging.getLogger("PIL").setLevel(logging.WARNING)  # chunk-level decoder chatter, no use in a trace

WordSource = Literal["text_layer", "ocr"]

#: 8-point text lands at ~22 px, above Textract's 15 px minimum (ADR-007 s6).
DEFAULT_RENDER_DPI = 200

#: Textract's synchronous limits (limits-document.html): 10 MB, 10000 px a side.
OCR_MAX_BYTES = 10 * 1024 * 1024
OCR_MAX_SIDE_PX = 10_000

#: Fixed reason codes. Logged; never a raw exception message.
OCR_UNAVAILABLE = "ocr_unavailable"
OCR_TIMEOUT = "ocr_timeout"
OCR_FAILED = "ocr_failed"


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
    """Reads one upright page image. TextractOcr in production, FakeOcr in CI.

    Returned boxes are normalised to the image it was given - which is the page
    frame, because we render (or orient) that image ourselves.
    """

    async def read_page(self, png: bytes, *, page: int) -> tuple[Word, ...]: ...


class OcrError(Exception):
    """OCR could not read a page. ``reason`` is a fixed code, safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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


# --------------------------------------------------------------------------- #
# Images for OCR: rendered PDF pages and upright photos
# --------------------------------------------------------------------------- #


def _encode(image: Image.Image, fmt: str) -> bytes:
    buf = io.BytesIO()
    if fmt == "JPEG":
        if image.mode not in ("L", "RGB"):
            image = image.convert("RGB")
        image.save(buf, format="JPEG", quality=90)
    else:
        image.save(buf, format="PNG")
    return buf.getvalue()


def render_page_png(pdf_bytes: bytes, page: int, *, dpi: int = DEFAULT_RENDER_DPI) -> bytes:
    """One page as a grayscale PNG: the cropbox with /Rotate applied - the viewer's frame."""
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(pdf_bytes)
        try:
            image = document[page - 1].render(scale=dpi / 72, grayscale=True).to_pil()
        finally:
            document.close()
    return _encode(image, "PNG")


def pdf_page_count(pdf_bytes: bytes) -> int | None:
    """Pages in a PDF, or None when pdfium cannot open it (the extractor then reports it)."""
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(pdf_bytes)
        except Exception:  # noqa: BLE001 - an unreadable PDF is the extractor's to report, with its own code
            return None
        try:
            return len(document)
        finally:
            document.close()


def warm_renderer() -> None:
    """Pay pdfium's first-render cost (~5 s in a fresh process) at startup, not on a request."""
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument.new()
        try:
            document.new_page(72, 72)
            document[0].render(scale=1, grayscale=True).to_pil()
        finally:
            document.close()


def orient_image(data: bytes) -> bytes | None:
    """The photo turned upright from its EXIF orientation, within OCR's size limits.

    A browser shows a photo upright, so boxes must be measured on the upright
    image or they land in the wrong place in the preview. The bytes come back
    unchanged when nothing needs changing; None when they are not an image.
    """
    try:
        with Image.open(io.BytesIO(data)) as opened:
            fmt = opened.format or "PNG"
            orientation = opened.getexif().get(0x0112, 1)
            image = ImageOps.exif_transpose(opened)
            image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None

    changed = orientation not in (None, 1)
    longest = max(image.size)
    if longest > OCR_MAX_SIDE_PX:
        scale = OCR_MAX_SIDE_PX / longest
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
        changed = True
    if not changed and len(data) <= OCR_MAX_BYTES and fmt in ("PNG", "JPEG"):
        return data
    out = _encode(image, "JPEG" if fmt == "JPEG" else "PNG")
    while len(out) > OCR_MAX_BYTES and min(image.size) > 64:
        image = image.resize((image.width * 3 // 4, image.height * 3 // 4))
        out = _encode(image, "JPEG")
    return out


# --------------------------------------------------------------------------- #
# The fake OCR source: what CI runs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OcrCall:
    page: int
    byte_count: int
    size: tuple[int, int]


@dataclass
class FakeOcr:
    """Deterministic, offline OCR (the FakeReranker pattern).

    Returns the words it was scripted with for each page, re-stamped with the
    page and ``source="ocr"``. With no script it reads nothing, which leaves
    every OCR-dependent value unverified - the honest answer when no OCR ran.
    ``available=False`` drives the same failure path an AWS outage would.
    ``calls`` records what it was asked to read (page, byte count, pixel size).
    """

    pages: Mapping[int, Sequence[Word]] = field(default_factory=dict)
    available: bool = True
    calls: list[OcrCall] = field(default_factory=list)

    name = "fake-ocr"

    async def read_page(self, png: bytes, *, page: int) -> tuple[Word, ...]:
        size = (0, 0)
        if png:
            try:
                with Image.open(io.BytesIO(png)) as image:
                    size = image.size
            except (UnidentifiedImageError, OSError):
                pass
        self.calls.append(OcrCall(page=page, byte_count=len(png), size=size))
        if not self.available:
            raise OcrError(OCR_UNAVAILABLE)
        return tuple(Word(text=w.text, page=page, bbox=w.bbox, source="ocr") for w in self.pages.get(page, ()))


# --------------------------------------------------------------------------- #
# The Textract adapter: with reranker.py, one of the two places boto3 may appear
# --------------------------------------------------------------------------- #

#: ADR-007 s11a: the organisation's service control policy allows Textract in
#: us-east-2 only (it is denied in us-east-1 and us-west-2). Independent of the
#: Bedrock region.
DEFAULT_TEXTRACT_REGION = "us-east-2"
DEFAULT_TEXTRACT_TIMEOUT_SECONDS = 10.0
DEFAULT_TEXTRACT_MAX_ATTEMPTS = 2
TEXTRACT_RETRY_BASE_SECONDS = 0.2

#: Errors a retry cannot fix: permissions, region policy, a document Textract rejects.
_PERMANENT_TEXTRACT_ERRORS = frozenset({
    "AccessDeniedException",
    "UnrecognizedClientException",
    "InvalidSignatureException",
    "ExpiredTokenException",
    "InvalidParameterException",
    "UnsupportedDocumentException",
    "BadDocumentException",
    "DocumentTooLargeException",
    "ValidationException",
})


def _aws_error_code(exc: Exception) -> str | None:
    """botocore's ``ClientError`` carries a fixed code such as ``ThrottlingException``."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        return str(code) if code else None
    return None


async def _backoff_sleep(seconds: float) -> None:  # patched out in tests
    await asyncio.sleep(seconds)


class TextractOcr:
    """AWS Textract ``DetectDocumentText``, synchronous, one page image per call.

    Timeouts and retries: botocore's own retries are off; this adapter makes at
    most ``max_attempts`` calls with jittered backoff, all inside one
    ``timeout_seconds`` budget per page. Permissions, region-policy and
    rejected-document errors are not retried. Every failure is an
    :class:`OcrError` with a fixed code, which the caller turns into unverified
    values. Logs carry the exception class and AWS error code - never the
    message, never a word.

    ``client`` injects a stand-in for tests; otherwise boto3 is imported on the
    first call (lazily, like the reranker, so importing this module needs no SDK).
    """

    name = "aws-textract"

    def __init__(
        self,
        *,
        client: Any | None = None,
        region: str = DEFAULT_TEXTRACT_REGION,
        timeout_seconds: float = DEFAULT_TEXTRACT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_TEXTRACT_MAX_ATTEMPTS,
    ) -> None:
        self.region = region
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self._client = client

    def client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import boto3  # noqa: PLC0415 - lazy by design (ADR-007 s8)
            from botocore.config import Config  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - boto3 is a declared dependency
            raise OcrError(OCR_UNAVAILABLE) from exc
        self._client = boto3.client(
            "textract",
            region_name=self.region,
            config=Config(
                read_timeout=self.timeout_seconds,
                connect_timeout=self.timeout_seconds,
                retries={"max_attempts": 0},  # retry is bounded here, not by botocore
            ),
        )
        return self._client

    async def read_page(self, png: bytes, *, page: int) -> tuple[Word, ...]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        for attempt in range(1, self.max_attempts + 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                client = self.client()
                response = await asyncio.wait_for(
                    asyncio.to_thread(client.detect_document_text, Document={"Bytes": png}), remaining
                )
            except TimeoutError:
                log_event("ocr.textract_timeout", page=page, attempt=attempt, region=self.region)
                raise OcrError(OCR_TIMEOUT) from None
            except OcrError:
                raise
            except Exception as exc:  # noqa: BLE001 - every fault ends in one fixed code
                code = _aws_error_code(exc)
                log_event("ocr.textract_error", page=page, attempt=attempt, error_type=type(exc).__name__,
                          reason_code=code, region=self.region)
                if code in _PERMANENT_TEXTRACT_ERRORS or attempt >= self.max_attempts:
                    raise OcrError(OCR_UNAVAILABLE) from None
                pause = TEXTRACT_RETRY_BASE_SECONDS * (2 ** (attempt - 1)) * (0.5 + random.random())  # noqa: S311
                if pause >= deadline - loop.time():
                    raise OcrError(OCR_TIMEOUT) from None
                await _backoff_sleep(pause)
                continue
            return self._words(response, page)
        raise OcrError(OCR_TIMEOUT)

    @staticmethod
    def _words(response: Any, page: int) -> tuple[Word, ...]:
        """WORD blocks as Words. Textract boxes are already 0-1 of the image we sent."""
        try:
            words: list[Word] = []
            for block in response["Blocks"]:
                if block.get("BlockType") != "WORD":
                    continue
                text = str(block.get("Text", "")).strip()
                geometry = block["Geometry"]["BoundingBox"]
                left, top = float(geometry["Left"]), float(geometry["Top"])
                box = normalised_box(
                    left, top, left + float(geometry["Width"]), top + float(geometry["Height"]),
                    width=1.0, height=1.0,
                )
                if text and box is not None:  # partly off the image: dropped, never clamped
                    words.append(Word(text=text, page=page, bbox=box, source="ocr"))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise OcrError(OCR_FAILED) from None
        return tuple(words)


_TEXTRACT_SOURCES: dict[tuple[str, float], TextractOcr] = {}


def default_ocr_source() -> OcrSource:
    """The OCR source extraction uses when the caller names none (``COPILOT_OCR``, default fake).

    One TextractOcr per (region, timeout) is kept, so its boto3 client and
    credential lookup are paid once per process rather than per document.
    """
    from app.settings import ServiceSettings  # noqa: PLC0415 - read at call time, so env changes apply

    settings = ServiceSettings()
    if settings.ocr != "textract":
        return FakeOcr()
    key = (settings.textract_region, settings.textract_timeout_seconds)
    if key not in _TEXTRACT_SOURCES:
        _TEXTRACT_SOURCES[key] = TextractOcr(region=key[0], timeout_seconds=key[1])
    return _TEXTRACT_SOURCES[key]


def configured_render_dpi() -> int:
    """``COPILOT_OCR_RENDER_DPI`` (default 200)."""
    from app.settings import ServiceSettings  # noqa: PLC0415

    return ServiceSettings().ocr_render_dpi


__all__ = [
    "DEFAULT_RENDER_DPI",
    "DEFAULT_TEXTRACT_REGION",
    "DEFAULT_TEXTRACT_TIMEOUT_SECONDS",
    "OCR_FAILED",
    "OCR_MAX_BYTES",
    "OCR_MAX_SIDE_PX",
    "OCR_TIMEOUT",
    "OCR_UNAVAILABLE",
    "FakeOcr",
    "OcrCall",
    "OcrError",
    "OcrSource",
    "TextractOcr",
    "PageWords",
    "Word",
    "WordSource",
    "normalised_box",
    "configured_render_dpi",
    "default_ocr_source",
    "orient_image",
    "render_page_png",
    "text_layer_pages",
    "warm_renderer",
]
