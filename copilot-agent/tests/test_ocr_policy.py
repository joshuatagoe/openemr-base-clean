"""ADR-007 OCR policy: which pages are read by OCR, in what frame, and what failure does.

  * a PDF page with no text layer is OCR'd (rendered by us at 200 DPI);
  * a photo is OCR'd after being turned upright from its EXIF orientation;
  * a page WITH a text layer is OCR'd only as a fallback - when a value was not
    found on it and the page carries images (a printed form, handwritten value);
  * an OCR failure makes the affected values unverified; extraction never fails.

FakeOcr is scripted with word boxes measured from the fixture's own render, so
"the box lands on the ink" is checked in the frame the OCR source really sees.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageOps

from app.documents import VerificationStatus
from app.lab_extractor import LabDraft, LabResultDraft, extract_lab_document
from app.page_text import FakeOcr, OcrError, Word, orient_image, render_page_png, warm_renderer
from tests.fakes import FakeProvider
from tests.test_page_text import ink

pytestmark = pytest.mark.anyio

DOCS = Path(__file__).resolve().parent.parent / "fixtures" / "documents"
CLEAN = DOCS / "lab_hba1c_clean.pdf"
IMAGE_ONLY = DOCS / "verification" / "v_image_only.pdf"
FORM = DOCS / "verification" / "v_form_handwritten.pdf"
PHOTO = DOCS / "verification" / "v_photo_exif.jpg"


def _ink_box(img: Image.Image, region: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Normalised bounding box of the dark pixels inside ``region`` - what an OCR engine would report."""
    w, h = img.size
    x0, y0, x1, y1 = region
    crop = img.convert("L").crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    left, top, right, bottom = ImageOps.invert(crop).getbbox()
    return ((int(x0 * w) + left) / w, (int(y0 * h) + top) / h, (int(x0 * w) + right) / w, (int(y0 * h) + bottom) / h)


def _ocr_word(text: str, bbox: tuple[float, float, float, float], page: int = 1) -> Word:
    return Word(text=text, page=page, bbox=bbox, source="ocr")


def _draft(value: str, *, name: str = "Hemoglobin A1c", unit: str = "%") -> LabDraft:
    row = LabResultDraft(test_name=name, value_text=value, unit=unit, reference_range="4.0-5.6",
                         quote=value, page=1)
    return LabDraft(collection_date="2026-09-12", page_count=1, results=[row])


async def _extract(document: Path, value: str, ocr: FakeOcr, media_type: str = "application/pdf"):
    return await extract_lab_document(
        document_id=9, pdf_bytes=document.read_bytes(), media_type=media_type,
        provider=FakeProvider(_draft(value)), ocr=ocr,
    )


# --------------------------------------------------------------------------- #
# Pages with no text layer
# --------------------------------------------------------------------------- #


async def test_an_image_only_page_is_rendered_at_200_dpi_and_read_by_ocr() -> None:
    render = Image.open(io.BytesIO(render_page_png(IMAGE_ONLY.read_bytes(), 1)))
    value_box = _ink_box(render, (0.30, 0.285, 0.34, 0.303))
    name_box = _ink_box(render, (0.11, 0.285, 0.25, 0.303))
    ocr = FakeOcr({1: [_ocr_word("Hemoglobin", name_box), _ocr_word("8.9", value_box)]})

    doc = await _extract(IMAGE_ONLY, "8.9", ocr)

    (result,) = doc.results
    assert result.verification_status is VerificationStatus.VERIFIED_EXACT
    assert result.citation.page == 1 and result.citation.bbox == value_box
    assert ink(render, result.citation.bbox) > 0.1
    (call,) = ocr.calls
    assert call.page == 1
    assert call.size == (1700, 2200), "Letter at 200 DPI"


async def test_an_image_only_page_that_ocr_cannot_read_leaves_the_value_unverified() -> None:
    doc = await _extract(IMAGE_ONLY, "8.9", FakeOcr())
    (result,) = doc.results
    assert result.verification_status is VerificationStatus.UNVERIFIED and result.citation.bbox is None


async def test_an_ocr_failure_degrades_to_unverified_and_never_fails_the_extraction(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    doc = await _extract(IMAGE_ONLY, "8.9", FakeOcr(available=False))
    (result,) = doc.results
    assert result.verification_status is VerificationStatus.UNVERIFIED
    assert any(getattr(r, "reason_code", None) == "ocr_unavailable" for r in caplog.records)


# --------------------------------------------------------------------------- #
# Pages with a text layer
# --------------------------------------------------------------------------- #


async def test_a_text_page_without_images_is_never_sent_to_ocr() -> None:
    ocr = FakeOcr()
    doc = await _extract(CLEAN, "9.9", ocr)  # a miss, but there is nothing on the page OCR could add
    assert doc.results[0].verification_status is VerificationStatus.UNVERIFIED
    assert ocr.calls == []


async def test_a_value_found_in_the_text_layer_needs_no_ocr() -> None:
    ocr = FakeOcr()
    doc = await _extract(FORM, "4.0-5.6", ocr)
    assert doc.results[0].verification_status is VerificationStatus.VERIFIED_EXACT
    assert ocr.calls == []


async def test_a_handwritten_value_on_a_printed_form_is_found_by_the_ocr_fallback() -> None:
    render = Image.open(io.BytesIO(render_page_png(FORM.read_bytes(), 1)))
    # The value image sits at x 252-300 pt, y 548-566 pt (from the bottom) of a 612x792 page.
    value_box = _ink_box(render, (252 / 612, (792 - 566) / 792, 300 / 612, (792 - 548) / 792))
    ocr = FakeOcr({1: [_ocr_word("7.4", value_box)]})

    doc = await _extract(FORM, "7.4", ocr)

    (result,) = doc.results
    assert result.verification_status is VerificationStatus.VERIFIED_EXACT
    assert result.citation.bbox == value_box
    assert ink(render, result.citation.bbox) > 0.1
    assert [c.page for c in ocr.calls] == [1], "the fallback reads the page once"


async def test_the_fallback_still_refuses_a_value_the_ocr_did_not_see() -> None:
    ocr = FakeOcr({1: [_ocr_word("7.4", (0.4, 0.29, 0.45, 0.305))]})
    doc = await _extract(FORM, "7.9", ocr)
    assert doc.results[0].verification_status is VerificationStatus.UNVERIFIED
    assert len(ocr.calls) == 1


async def test_ocr_text_never_reaches_a_log(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    ocr = FakeOcr({1: [_ocr_word("CANARYWORD", (0.1, 0.1, 0.2, 0.12))]})
    await _extract(IMAGE_ONLY, "8.9", ocr)
    assert "CANARYWORD" not in caplog.text
    assert "v_image_only" not in caplog.text


# --------------------------------------------------------------------------- #
# Photos
# --------------------------------------------------------------------------- #


def test_orient_image_turns_an_exif_rotated_photo_upright() -> None:
    raw = Image.open(PHOTO)
    assert raw.size == (560, 850), "stored sideways"
    upright = Image.open(io.BytesIO(orient_image(PHOTO.read_bytes())))
    assert upright.size == (850, 560)
    # The heading is near the top of the upright page, not down the side.
    assert ink(upright, (0.10, 0.15, 0.50, 0.20)) > 0.05
    assert ink(upright, (0.0, 0.5, 0.08, 1.0)) < 0.01


def test_orient_image_refuses_bytes_that_are_not_an_image() -> None:
    assert orient_image(b"not an image") is None


async def test_a_photo_is_ocrd_upright_and_its_box_is_in_the_upright_frame() -> None:
    upright = Image.open(io.BytesIO(orient_image(PHOTO.read_bytes())))
    value_box = _ink_box(upright, (0.29, 0.56, 0.33, 0.595))
    name_box = _ink_box(upright, (0.11, 0.56, 0.25, 0.595))
    ocr = FakeOcr({1: [_ocr_word("Hemoglobin", name_box), _ocr_word("8.9", value_box)]})

    doc = await _extract(PHOTO, "8.9", ocr, media_type="image/jpeg")

    (result,) = doc.results
    assert result.verification_status is VerificationStatus.VERIFIED_EXACT
    assert ink(upright, result.citation.bbox) > 0.1
    (call,) = ocr.calls
    assert call.size == (850, 560), "OCR saw the upright image, not the stored pixels"


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #


def test_warm_renderer_runs() -> None:
    warm_renderer()


def test_render_draws_the_cropbox_rotated_like_the_viewer() -> None:
    rotated = DOCS / "verification" / "v_rotated.pdf"
    img = Image.open(io.BytesIO(render_page_png(rotated.read_bytes(), 1)))
    assert img.size == (2200, 1700)
    direct = pdfium.PdfDocument(rotated.read_bytes())[0].render(scale=200 / 72).to_pil()
    assert img.size == direct.size


async def test_fake_ocr_raises_the_fixed_error_when_unavailable() -> None:
    with pytest.raises(OcrError) as caught:
        await FakeOcr(available=False).read_page(b"", page=1)
    assert caught.value.reason == "ocr_unavailable"
