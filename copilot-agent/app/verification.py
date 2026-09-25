"""The one matcher: is this extracted value printed on the page, and where? (ADR-007)

Verification comes from here and nowhere else. The model reads the page; this
module checks the reading against the words the page actually carries (text
layer or OCR, :mod:`app.page_text`). Found gives ``verified_exact`` or
``verified_fuzzy`` with a page and a box; anything else gives ``unverified``
with no box. A box is never guessed.

The rule, in order:

1. **Candidates.** Every run of 1-4 consecutive words on one line whose text
   equals the value. *Exact*: the words joined by single spaces equal the value
   with its whitespace collapsed. *Fuzzy*, tried only when no exact candidate
   survives step 2, is deliberately narrow - it forgives presentation, never a
   different value:
     - case and internal whitespace ("NEGATIVE" for "Negative");
     - surrounding punctuation ``* ( ) [ ] { } , ; :`` and quotes ("(8.9)", "8.9*");
     - a printed flag glued to a number (H, L, HH, LL, A: "8.9H");
     - the same finite number at another printed precision ("8.90" for "8.9").
   It never accepts a different digit, a lost decimal point, a truncation, part
   of another token ("B12" is not "12"), a range bound ("4.0-8.9" is not
   "8.9"), or an OCR confusion ("8.O" is not "8.0").
2. **Row anchor.** The test name's words (letters/digits, two or more
   characters, not pure numbers) are looked for on each candidate's line. Only
   the candidates whose line carries the most name words survive. If none
   carries any, the candidates survive only when the name appears nowhere on the
   searched pages (a report printing "HbA1c" for "Hemoglobin A1c"); otherwise
   the value is on some other test's row and is not verified for this one.
3. **Uniqueness.** Candidates that are the same word read twice (text layer and
   OCR of the same page) collapse into one. More than one left: the page hint
   (the page the model cited) is preferred; still more than one: ambiguous, not
   verified. Two equal readings are not evidence of which one is meant.

A "line" follows the page's reading direction: on a page whose text runs down
the page (``/Rotate 90`` displays that way) a line is a column.

PHI: nothing here logs.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app.documents import LabResult, VerificationStatus
from app.observability import log_event
from app.page_text import (
    DEFAULT_RENDER_DPI,
    OCR_FAILED,
    OcrError,
    OcrSource,
    PageWords,
    Word,
    orient_image,
    render_page_png,
    text_layer_pages,
)

MAX_SPAN_WORDS = 4

_EDGE_PUNCTUATION = "*()[]{},;:\"'‘’“”"
_GLUED_FLAGS = ("hh", "ll", "h", "l", "a")
_TOKEN = re.compile(r"[a-z0-9]+")


class MatchKind(StrEnum):
    EXACT = "exact"
    FUZZY = "fuzzy"


@dataclass(frozen=True)
class Match:
    kind: MatchKind
    page: int
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class _Span:
    words: tuple[Word, ...]
    page: PageWords

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (
            min(w.bbox[0] for w in self.words),
            min(w.bbox[1] for w in self.words),
            max(w.bbox[2] for w in self.words),
            max(w.bbox[3] for w in self.words),
        )


# --------------------------------------------------------------------------- #
# Text comparison
# --------------------------------------------------------------------------- #


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _loose(text: str) -> str:
    return "".join(text.split()).casefold().strip(_EDGE_PUNCTUATION)


def _number(text: str) -> Decimal | None:
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def is_exact(span_text: str, value: str) -> bool:
    return span_text == _collapse(value)


def is_fuzzy(span_text: str, value: str) -> bool:
    printed, wanted = _loose(span_text), _loose(value)
    if not printed or not wanted:
        return False
    if printed == wanted:
        return True
    wanted_number = _number(wanted)
    if wanted_number is None:
        return False
    for flag in _GLUED_FLAGS:
        if printed.endswith(flag) and printed[: -len(flag)] == wanted:
            return True
    printed_number = _number(printed)
    return printed_number is not None and printed_number == wanted_number


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def _is_vertical(page: PageWords) -> bool:
    """Text runs down the page when most multi-character words are taller than wide."""
    votes = [
        (w.bbox[3] - w.bbox[1]) > (w.bbox[2] - w.bbox[0]) for w in page.words if len(w.text) >= 3
    ]
    return bool(votes) and sum(votes) * 2 > len(votes)


def _same_line(a: Word, b: Word, *, vertical: bool) -> bool:
    lo, hi = (0, 2) if vertical else (1, 3)
    overlap = min(a.bbox[hi], b.bbox[hi]) - max(a.bbox[lo], b.bbox[lo])
    extent = min(a.bbox[hi] - a.bbox[lo], b.bbox[hi] - b.bbox[lo])
    return extent > 0 and overlap >= 0.5 * extent


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


# --------------------------------------------------------------------------- #
# The matcher
# --------------------------------------------------------------------------- #


def _name_tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.casefold()) if len(t) >= 2 and not t.isdigit()}


def _spans(page: PageWords) -> list[_Span]:
    vertical = _is_vertical(page)
    words = page.words
    out: list[_Span] = []
    for i, first in enumerate(words):
        run = [first]
        out.append(_Span((first,), page))
        for nxt in words[i + 1 : i + MAX_SPAN_WORDS]:
            if not _same_line(first, nxt, vertical=vertical):
                break
            run.append(nxt)
            out.append(_Span(tuple(run), page))
    return out


def _anchor_score(span: _Span, tokens: set[str]) -> int:
    vertical = _is_vertical(span.page)
    own = set(span.words)
    line_tokens: set[str] = set()
    for word in span.page.words:
        if word not in own and _same_line(span.words[0], word, vertical=vertical):
            line_tokens |= _name_tokens(word.text)
    return len(tokens & line_tokens)


def _dedupe(spans: list[_Span]) -> list[_Span]:
    """The same printed word read by the text layer and by OCR is one candidate, not two."""
    kept: list[_Span] = []
    for span in sorted(spans, key=lambda s: s.words[0].source != "text_layer"):
        if not any(k.page.page == span.page.page and _iou(k.bbox, span.bbox) > 0.5 for k in kept):
            kept.append(span)
    return kept


def _choose(
    candidates: list[_Span], tokens: set[str], pages: Sequence[PageWords], page_hint: int | None
) -> _Span | None:
    if not candidates:
        return None
    scored = [(_anchor_score(c, tokens), c) for c in candidates]
    best = max(score for score, _ in scored)
    if best > 0:
        survivors = [c for score, c in scored if score == best]
    else:
        name_on_page = any(tokens & _name_tokens(w.text) for p in pages for w in p.words)
        if name_on_page:
            return None  # the value sits on another test's row
        survivors = candidates
    survivors = _dedupe(survivors)
    if len(survivors) > 1 and page_hint is not None:
        hinted = [c for c in survivors if c.page.page == page_hint]
        survivors = hinted or survivors
    return survivors[0] if len(survivors) == 1 else None


def find_value(
    value: str, test_name: str, pages: Sequence[PageWords], *, page_hint: int | None = None
) -> Match | None:
    """Locate ``value`` on the row named ``test_name``, or None. See the module rule."""
    if not value.strip():
        return None
    tokens = _name_tokens(test_name)
    spans = [s for page in pages for s in _spans(page)]
    texts = [(" ".join(w.text for w in s.words), s) for s in spans]
    for kind, accept in ((MatchKind.EXACT, is_exact), (MatchKind.FUZZY, is_fuzzy)):
        chosen = _choose([s for text, s in texts if accept(text, value)], tokens, pages, page_hint)
        if chosen is not None:
            return Match(kind=kind, page=chosen.page.page, bbox=chosen.bbox)
    return None


# --------------------------------------------------------------------------- #
# Applying it to results
# --------------------------------------------------------------------------- #


def printed_value(result: LabResult) -> str | None:
    """The value as text, at its printed precision (``Decimal('8.90')`` stays "8.90")."""
    if result.value is None:
        return None
    return format(result.value, "f") if isinstance(result.value, Decimal) else str(result.value)


def verify_result(
    result: LabResult, pages: Sequence[PageWords], *, page_hint: int | None = None
) -> LabResult:
    """The result with its status, page and box set by the matcher - whatever they were before.

    ``unreadable`` is left unreadable (no value to look for) and never gets a box.
    """
    if result.verification_status is VerificationStatus.UNREADABLE:
        status, page, bbox = VerificationStatus.UNREADABLE, None, None
    else:
        text = printed_value(result)
        match = find_value(text, result.test_name, pages, page_hint=page_hint) if text else None
        if match is None:
            status, page, bbox = VerificationStatus.UNVERIFIED, None, None
        else:
            status = (
                VerificationStatus.VERIFIED_EXACT if match.kind is MatchKind.EXACT
                else VerificationStatus.VERIFIED_FUZZY
            )
            page, bbox = match.page, match.bbox
    citation = {**result.citation.model_dump(), "page": page, "bbox": bbox}
    return LabResult.model_validate({**result.model_dump(), "verification_status": status, "citation": citation})


# --------------------------------------------------------------------------- #
# The document: which pages are read, and by what (ADR-007 s2)
# --------------------------------------------------------------------------- #

#: At most this many OCR calls in flight per document: bounded fan-out, well
#: under Textract's 25 TPS quota, and a many-page scan cannot flood it.
OCR_CONCURRENCY = 4


@dataclass
class VerificationStats:
    """Counts only - the span/log payload. Never a word, value or page image."""

    page_count: int = 0
    ocr_pages: int = 0
    ocr_fallback_pages: int = 0
    ocr_failed_pages: int = 0
    ocr_failure_reasons: list[str] = field(default_factory=list)
    verified_exact: int = 0
    verified_fuzzy: int = 0
    unverified: int = 0
    unreadable: int = 0


def _upright_image(document: bytes, media_type: str, page: int, dpi: int) -> bytes | None:
    if media_type == "application/pdf":
        return render_page_png(document, page, dpi=dpi)
    return orient_image(document)


async def _ocr_pages(
    numbers: Sequence[int],
    *,
    document: bytes,
    media_type: str,
    ocr: OcrSource,
    dpi: int,
    stats: VerificationStats,
) -> dict[int, tuple[Word, ...]]:
    """OCR each page; a page that fails contributes no words (its values stay unverified)."""
    gate = asyncio.Semaphore(OCR_CONCURRENCY)

    async def one(number: int) -> tuple[int, tuple[Word, ...]]:
        async with gate:
            try:
                image = await asyncio.to_thread(_upright_image, document, media_type, number, dpi)
                if image is None:
                    raise OcrError(OCR_FAILED)
                words = await ocr.read_page(image, page=number)
            except OcrError as exc:
                reason = exc.reason
            except Exception as exc:  # noqa: BLE001 - OCR never fails the extraction (ADR-007 s2)
                reason = OCR_FAILED
                log_event("verification.ocr_error", page=number, error_type=type(exc).__name__)
            else:
                return number, tuple(w for w in words if w.page == number)
            stats.ocr_failed_pages += 1
            stats.ocr_failure_reasons.append(reason)
            log_event("verification.ocr_page_failed", page=number, reason_code=reason)
            return number, ()

    return dict(await asyncio.gather(*(one(n) for n in numbers)))


def _merge(page: PageWords, ocr_words: tuple[Word, ...]) -> PageWords:
    return PageWords(page=page.page, has_text_layer=page.has_text_layer, has_images=page.has_images,
                     words=page.words + ocr_words)


async def verify_document(
    results: Sequence[LabResult],
    page_hints: Sequence[int | None],
    *,
    document: bytes,
    media_type: str,
    ocr: OcrSource,
    dpi: int = DEFAULT_RENDER_DPI,
) -> tuple[list[LabResult], VerificationStats]:
    """Every result verified against the page words, reading pages by OCR where the policy says.

    1. PDF pages are read from the text layer. Pages with no text layer - and a
       photo, which has none - are OCR'd up front.
    2. A value not found on a page that has a text layer AND images (a printed
       form with a handwritten or stamped value) triggers OCR of that page - the
       page the model cited, or every such page when it cited none that exists -
       and the missed values are matched again.
    3. OCR failure leaves the affected values unverified. Nothing here raises.
    """
    stats = VerificationStats()
    if media_type == "application/pdf":
        pages = {p.page: p for p in await asyncio.to_thread(text_layer_pages, document)}
    else:
        pages = {1: PageWords(page=1, has_text_layer=False, has_images=True, words=())}
    stats.page_count = len(pages)

    upfront = [n for n, p in pages.items() if not p.has_text_layer]
    if upfront:
        stats.ocr_pages += len(upfront)
        read = await _ocr_pages(upfront, document=document, media_type=media_type, ocr=ocr, dpi=dpi, stats=stats)
        for number, words in read.items():
            pages[number] = _merge(pages[number], words)
    ocr_done = set(upfront)

    out = [verify_result(r, list(pages.values()), page_hint=h) for r, h in zip(results, page_hints, strict=True)]

    missed = [i for i, r in enumerate(out) if r.verification_status is VerificationStatus.UNVERIFIED and r.value is not None]
    candidates = {n for n, p in pages.items() if p.has_text_layer and p.has_images and n not in ocr_done}
    fallback: set[int] = set()
    for i in missed:
        hint = page_hints[i]
        fallback |= {hint} & candidates if hint in pages else candidates
    if fallback:
        numbers = sorted(fallback)
        stats.ocr_pages += len(numbers)
        stats.ocr_fallback_pages += len(numbers)
        read = await _ocr_pages(numbers, document=document, media_type=media_type, ocr=ocr, dpi=dpi, stats=stats)
        for number, words in read.items():
            pages[number] = _merge(pages[number], words)
        for i in missed:
            out[i] = verify_result(out[i], list(pages.values()), page_hint=page_hints[i])

    for r in out:
        if r.verification_status is VerificationStatus.VERIFIED_EXACT:
            stats.verified_exact += 1
        elif r.verification_status is VerificationStatus.VERIFIED_FUZZY:
            stats.verified_fuzzy += 1
        elif r.verification_status is VerificationStatus.UNREADABLE:
            stats.unreadable += 1
        else:
            stats.unverified += 1
    return out, stats


__all__ = [
    "MAX_SPAN_WORDS",
    "OCR_CONCURRENCY",
    "Match",
    "MatchKind",
    "VerificationStats",
    "find_value",
    "verify_document",
    "is_exact",
    "is_fuzzy",
    "printed_value",
    "verify_result",
]
