"""ADR-007: word boxes from the PDF text layer land on the ink the viewer draws.

"On the ink" is checked, not assumed: each fixture page is rendered with
pypdfium2 (which, like pdf.js, draws the cropbox with /Rotate applied), and the
fraction of dark pixels inside every normalised word box must be well above a
blank page's. A box in the wrong frame - mediabox instead of cropbox, unrotated
instead of rotated - lands on white paper and fails.
"""

from __future__ import annotations

import io
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image

from app.page_text import PageWords, Word, text_layer_pages

DOCS = Path(__file__).resolve().parent.parent / "fixtures" / "documents"
VERIFY = DOCS / "verification"
CLEAN = DOCS / "lab_hba1c_clean.pdf"
ROTATED = VERIFY / "v_rotated.pdf"
CROPPED = VERIFY / "v_cropped.pdf"
IMAGE_ONLY = VERIFY / "v_image_only.pdf"
FORM = VERIFY / "v_form_handwritten.pdf"


def _render(pdf: Path, page: int = 1) -> Image.Image:
    return pdfium.PdfDocument(pdf.read_bytes())[page - 1].render(scale=200 / 72, grayscale=True).to_pil()


def ink(img: Image.Image, bbox: tuple[float, float, float, float]) -> float:
    """Fraction of dark pixels inside a normalised box."""
    w, h = img.size
    x0, y0, x1, y1 = bbox
    crop = img.convert("L").crop(
        (int(x0 * w), int(y0 * h), max(int(x1 * w), int(x0 * w) + 1), max(int(y1 * h), int(y0 * h) + 1))
    )
    pixels = list(crop.getdata())
    return sum(1 for v in pixels if v < 128) / max(1, len(pixels))


def _one(pages: tuple[PageWords, ...], text: str) -> Word:
    found = [w for p in pages for w in p.words if w.text == text]
    assert len(found) == 1, f"expected exactly one {text!r}, found {len(found)}"
    return found[0]


@pytest.mark.parametrize("pdf", [CLEAN, ROTATED, CROPPED], ids=["plain", "rotate90", "cropbox"])
def test_every_word_box_lands_on_rendered_ink(pdf: Path) -> None:
    pages = text_layer_pages(pdf.read_bytes())
    img = _render(pdf)
    words = pages[0].words
    assert len(words) > 20
    densities = [ink(img, w.bbox) for w in words]
    assert min(densities) > 0.03, "a word box sits on blank paper"
    assert sum(densities) / len(densities) > 0.10
    # Far denser than the page as a whole, so a box on paper cannot pass by chance.
    assert sum(densities) / len(densities) > 5 * ink(img, (0.0, 0.0, 1.0, 1.0))


def test_boxes_are_normalised_top_left_and_in_range() -> None:
    for pdf in (CLEAN, ROTATED, CROPPED):
        for page in text_layer_pages(pdf.read_bytes()):
            for w in page.words:
                x0, y0, x1, y1 = w.bbox
                assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
                assert w.page == page.page == 1
                assert w.source == "text_layer"


def test_top_left_origin_puts_the_header_above_the_results() -> None:
    pages = text_layer_pages(CLEAN.read_bytes())
    header = _one(pages, "NORTHSIDE")
    value = _one(pages, "8.9")
    assert header.bbox[1] < value.bbox[1]


def test_a_rotated_page_reports_boxes_in_the_displayed_frame() -> None:
    """/Rotate 90 displays the page landscape; the text runs down the right-hand side."""
    word = _one(text_layer_pages(ROTATED.read_bytes()), "NORTHSIDE")
    x0, y0, x1, y1 = word.bbox
    assert (y1 - y0) > (x1 - x0), "rotated text is taller than it is wide in the displayed frame"


def test_words_outside_the_cropbox_are_dropped_not_clamped() -> None:
    pages = text_layer_pages(CROPPED.read_bytes())
    assert all(w.text != "OUTSIDECROP" for w in pages[0].words)
    assert any(w.text == "8.9" for w in pages[0].words)


def test_an_image_only_page_has_no_text_layer_but_has_images() -> None:
    (page,) = text_layer_pages(IMAGE_ONLY.read_bytes())
    assert page == PageWords(page=1, has_text_layer=False, has_images=True, words=())


def test_a_printed_form_has_a_text_layer_images_and_no_value_word() -> None:
    (page,) = text_layer_pages(FORM.read_bytes())
    assert page.has_text_layer and page.has_images
    assert not any(w.text == "7.4" for w in page.words)
    assert any(w.text == "Hemoglobin" for w in page.words)


def test_a_plain_text_page_reports_no_images() -> None:
    (page,) = text_layer_pages(CLEAN.read_bytes())
    assert page.has_text_layer and not page.has_images


def test_a_broken_pdf_yields_no_pages_rather_than_raising() -> None:
    assert text_layer_pages(b"%PDF-1.4 this is not a pdf") == ()


def test_the_fixture_renders_are_real_images() -> None:
    """Guard for the helper itself: a blank render would make every ink test vacuous."""
    img = _render(CLEAN)
    assert ink(img, (0.0, 0.0, 1.0, 1.0)) > 0.002
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    assert buf.tell() > 1000
