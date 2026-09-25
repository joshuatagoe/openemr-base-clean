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

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app.documents import LabResult, VerificationStatus
from app.page_text import PageWords, Word

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


__all__ = [
    "MAX_SPAN_WORDS",
    "Match",
    "MatchKind",
    "find_value",
    "is_exact",
    "is_fuzzy",
    "printed_value",
    "verify_result",
]
