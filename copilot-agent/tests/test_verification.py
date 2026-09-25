"""ADR-007: one matcher decides verified / unverified. The model's claim never stands.

Unit tests drive the matcher over hand-built word lists, so each rule is pinned
by a case that would pass under a looser rule. Integration tests drive
``extract_lab_document`` with a scripted draft, so the only thing that can make
a value verified is the page.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from app.documents import VerificationStatus
from app.lab_extractor import LabDraft, LabResultDraft, extract_lab_document
from app.page_text import PageWords, Word
from app.verification import MatchKind, find_value
from tests.fakes import FakeProvider
from tests.test_page_text import ink

pytestmark = pytest.mark.anyio

DOCS = Path(__file__).resolve().parent.parent / "fixtures" / "documents"
CLEAN = DOCS / "lab_hba1c_clean.pdf"
DEGRADED = DOCS / "lab_hba1c_degraded_scan.pdf"
ROTATED = DOCS / "verification" / "v_rotated.pdf"
CROPPED = DOCS / "verification" / "v_cropped.pdf"


def _line(y: float, *texts: str, page: int = 1, x: float = 0.1) -> list[Word]:
    """Words laid left to right on one horizontal line at height ``y``."""
    words = []
    for text in texts:
        width = 0.012 * max(1, len(text))
        words.append(Word(text=text, page=page, bbox=(x, y, x + width, y + 0.012), source="text_layer"))
        x += width + 0.02
    return words


def _page(*lines: list[Word], page: int = 1) -> PageWords:
    words = tuple(w for line in lines for w in line)
    return PageWords(page=page, has_text_layer=True, has_images=False, words=words)


PANEL = _page(
    _line(0.10, "TEST", "RESULT", "UNITS", "RANGE"),
    _line(0.20, "Hemoglobin", "A1c", "8.9", "%", "4.0-5.6"),
    _line(0.23, "Glucose,", "Fasting", "164", "mg/dL", "70-99"),
    _line(0.26, "Creatinine", "0.9", "mg/dL", "0.6-1.1"),
)


# --------------------------------------------------------------------------- #
# Exact
# --------------------------------------------------------------------------- #


def test_an_exact_value_on_its_row_is_verified_with_that_words_box() -> None:
    match = find_value("8.9", "Hemoglobin A1c", [PANEL])
    assert match is not None
    assert match.kind is MatchKind.EXACT
    assert match.page == 1
    value_word = next(w for w in PANEL.words if w.text == "8.9")
    assert match.bbox == value_word.bbox


def test_a_value_absent_from_the_page_is_not_found() -> None:
    assert find_value("9.1", "Hemoglobin A1c", [PANEL]) is None


def test_a_value_printed_on_another_tests_row_is_not_verified_for_this_test() -> None:
    """The model put creatinine's 0.9 under glucose: finding '0.9' somewhere is not verification."""
    assert find_value("0.9", "Glucose, Fasting", [PANEL]) is None


def test_the_value_is_located_on_the_named_row_when_it_appears_twice() -> None:
    page = _page(
        _line(0.20, "Potassium", "4.1", "mmol/L", "3.5-5.1"),
        _line(0.23, "Albumin", "4.1", "g/dL", "3.5-5.0"),
    )
    match = find_value("4.1", "Albumin", [page])
    assert match is not None
    assert match.bbox[1] == pytest.approx(0.23)


def test_an_unanchored_value_that_appears_twice_is_ambiguous_and_not_verified() -> None:
    page = _page(_line(0.20, "4.1", "mmol/L"), _line(0.23, "4.1", "g/dL"))
    assert find_value("4.1", "Potassium", [page]) is None


def test_an_unanchored_value_that_appears_once_is_verified() -> None:
    """The report prints 'HbA1c'; the model wrote 'Hemoglobin A1c'. The number is still on the page once."""
    page = _page(_line(0.20, "HbA1c", "8.9", "%"))
    match = find_value("8.9", "Hemoglobin A1c", [page])
    assert match is not None and match.kind is MatchKind.EXACT


def test_a_multi_word_value_is_matched_and_boxed_as_one_span() -> None:
    page = _page(_line(0.30, "HIV", "antibody", "Not", "detected"))
    match = find_value("Not detected", "HIV antibody", [page])
    assert match is not None and match.kind is MatchKind.EXACT
    words = [w for w in page.words if w.text in ("Not", "detected")]
    assert match.bbox == (words[0].bbox[0], words[0].bbox[1], words[1].bbox[2], words[1].bbox[3])


def test_the_page_hint_breaks_a_tie_between_pages() -> None:
    p1 = _page(_line(0.2, "Sodium", "140", "mmol/L"), page=1)
    p2 = _page(_line(0.2, "Sodium", "140", "mmol/L", page=2), page=2)
    match = find_value("140", "Sodium", [p1, p2], page_hint=2)
    assert match is not None and match.page == 2
    assert find_value("140", "Sodium", [p1, p2]) is None, "without a hint two equal rows are ambiguous"


# --------------------------------------------------------------------------- #
# Fuzzy - the documented rule, and what it refuses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("printed", "value"),
    [
        ("8.9H", "8.9"),       # flag glued to the value
        ("8.9*", "8.9"),       # asterisk legend
        ("(8.9)", "8.9"),      # bracketed
        ("8.90", "8.9"),       # same number, printed precision differs
        ("NEGATIVE", "Negative"),  # case only
    ],
)
def test_fuzzy_matches_are_verified_fuzzy(printed: str, value: str) -> None:
    page = _page(_line(0.2, "Result", printed))
    match = find_value(value, "Result", [page])
    assert match is not None
    assert match.kind is MatchKind.FUZZY


@pytest.mark.parametrize(
    ("printed", "value"),
    [
        ("8.6", "8.9"),      # a different number is never "close enough"
        ("89", "8.9"),       # a lost decimal point is a different value
        ("8.9", "8"),        # a truncation is not a match
        ("B12", "12"),       # part of another token
        ("4.0-8.9", "8.9"),  # a range bound is not the value
        ("8.O", "8.0"),      # OCR confusions are not repaired
    ],
)
def test_fuzzy_never_accepts_a_different_value(printed: str, value: str) -> None:
    page = _page(_line(0.2, "Result", printed))
    assert find_value(value, "Result", [page]) is None


def test_an_exact_match_wins_over_a_fuzzy_one() -> None:
    page = _page(_line(0.2, "Result", "8.9H", "8.9"))
    match = find_value("8.9", "Result", [page])
    assert match is not None and match.kind is MatchKind.EXACT


def test_a_vertical_page_groups_lines_by_column() -> None:
    """On a /Rotate 90 page the text runs down the page, so a 'line' is a column."""
    def col(x: float, *texts: str) -> list[Word]:
        y, out = 0.1, []
        for t in texts:
            h = 0.012 * max(1, len(t))
            out.append(Word(text=t, page=1, bbox=(x, y, x + 0.012, y + h), source="text_layer"))
            y += h + 0.02
        return out

    page = _page(col(0.80, "Potassium", "4.1", "mmol/L"), col(0.77, "Albumin", "4.1", "g/dL"))
    match = find_value("4.1", "Albumin", [page])
    assert match is not None
    assert match.bbox[0] == pytest.approx(0.77)


# --------------------------------------------------------------------------- #
# Integration: the only source of a verified status is the page
# --------------------------------------------------------------------------- #


def _draft(*rows: LabResultDraft) -> LabDraft:
    return LabDraft(collection_date="2026-09-12", page_count=1, results=list(rows))


def _row(name: str, value: str, *, unit: str = "%", unreadable: bool = False) -> LabResultDraft:
    return LabResultDraft(test_name=name, value_text=value, unit=unit, reference_range="4.0-5.6",
                          quote=value, page=1, unreadable=unreadable)


async def test_a_value_the_model_read_but_the_page_does_not_print_is_unverified() -> None:
    """The self-report is gone: a legible-looking value the page never printed is not verified."""
    provider = FakeProvider(_draft(_row("Hemoglobin A1c", "9.4")))
    doc = await extract_lab_document(document_id=5, pdf_bytes=CLEAN.read_bytes(),
                                     media_type="application/pdf", provider=provider)
    (result,) = doc.results
    assert result.value == Decimal("9.4")
    assert result.verification_status is VerificationStatus.UNVERIFIED
    assert result.citation.bbox is None and result.citation.page is None
    assert doc.extraction_metadata.unverified_count == 1
    assert doc.extraction_metadata.verified_fraction == 0.0


@pytest.mark.parametrize("pdf", [CLEAN, ROTATED, CROPPED], ids=["plain", "rotate90", "cropbox"])
async def test_a_printed_value_is_verified_and_its_box_lands_on_the_ink(pdf: Path) -> None:
    provider = FakeProvider(_draft(_row("Hemoglobin A1c", "8.9")))
    doc = await extract_lab_document(document_id=5, pdf_bytes=pdf.read_bytes(),
                                     media_type="application/pdf", provider=provider)
    (result,) = doc.results
    assert result.verification_status is VerificationStatus.VERIFIED_EXACT
    assert result.citation.page == 1
    assert result.citation.bbox is not None
    img = pdfium.PdfDocument(pdf.read_bytes())[0].render(scale=200 / 72, grayscale=True).to_pil()
    assert ink(img, result.citation.bbox) > 0.1
    assert doc.extraction_metadata.verified_fraction == 1.0


async def test_an_unreadable_row_stays_unreadable_with_no_box() -> None:
    provider = FakeProvider(_draft(_row("Hemoglobin A1c", "8.#", unreadable=True)))
    doc = await extract_lab_document(document_id=5, pdf_bytes=DEGRADED.read_bytes(),
                                     media_type="application/pdf", provider=provider)
    (result,) = doc.results
    assert result.verification_status is VerificationStatus.UNREADABLE
    assert result.value is None and result.citation.bbox is None


async def test_the_offline_stub_verifies_the_clean_fixture_from_the_page() -> None:
    doc = await extract_lab_document(document_id=101, pdf_bytes=CLEAN.read_bytes(), media_type="application/pdf")
    statuses = {r.test_name: r.verification_status for r in doc.results}
    assert statuses["Hemoglobin A1c"] is VerificationStatus.VERIFIED_EXACT
    assert all(r.citation.bbox is not None for r in doc.results)


def test_the_extractor_no_longer_assigns_a_verified_status_itself() -> None:
    """Regression guard for the removed self-report (lab_extractor.py:394-396 before ADR-007)."""
    source = (Path(__file__).resolve().parent.parent / "app" / "lab_extractor.py").read_text(encoding="utf-8")
    # summarize() may count verified results; nothing else in the extractor may name the status.
    source = source.replace("(VerificationStatus.VERIFIED_EXACT, VerificationStatus.VERIFIED_FUZZY)", "")
    assert "VERIFIED_EXACT" not in source
    assert "VERIFIED_FUZZY" not in source
