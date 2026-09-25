"""Generate the ADR-007 geometry fixtures. Run: uv run python fixtures/documents/verification/_generate.py

All content is synthetic. Each file exists to break one assumption a naive
box-finder makes:

  v_rotated.pdf           /Rotate 90 - word boxes must be in the displayed frame
  v_cropped.pdf           cropbox smaller than the mediabox, one word outside it
  v_image_only.pdf        no text layer at all - only OCR can localise anything
  v_form_handwritten.pdf  printed labels in the text layer, the value an image
  v_photo_exif.jpg        pixels stored sideways, EXIF orientation 6 (rotate 90 CW)

PDFs are written by hand (as in ../_generate.py) so the bytes are
deterministic; images are drawn with Pillow's bundled scalable font.
"""

from __future__ import annotations

import io
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent

# (x, y, text, size); PDF user space, y measured from the bottom of a 612x792 page.
LINES = [
    (72, 720, "NORTHSIDE CLINICAL LABORATORY", 14),
    (72, 664, "PATIENT: Whitfield, Evelyn R.        DOB: 1981-03-14", 10),
    (72, 636, "ORDERING PROVIDER: Marcus Adeyemi, MD", 10),
    (72, 622, "COLLECTED: 2026-09-12 08:15", 10),
    (72, 588, "TEST                          RESULT      UNITS     REFERENCE RANGE", 10),
    (72, 558, "Hemoglobin A1c                8.9         %         4.0-5.6", 10),
    (72, 542, "Glucose, Fasting              164         mg/dL     70-99", 10),
    (72, 526, "Creatinine                    0.9         mg/dL     0.6-1.1", 10),
]


def _text_stream(lines: list[tuple[int, int, str, int]]) -> bytes:
    return "\n".join(
        f"BT /F1 {size} Tf {x} {y} Td ({text}) Tj ET" for x, y, text, size in lines
    ).encode("latin-1")


def _pdf(
    content: bytes,
    *,
    page_extra: str = "",
    image: tuple[int, int, bytes] | None = None,
) -> bytes:
    """One US-Letter page. ``image`` is (width, height, 8-bit gray pixels), drawn as /Im1."""
    resources = "/Font << /F1 5 0 R >>"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        None,  # page, filled below
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if image is not None:
        width, height, pixels = image
        data = zlib.compress(pixels, 9)
        objects.append(
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceGray "
            f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(data)} >>\nstream\n".encode()
            + data
            + b"\nendstream"
        )
        resources += " /XObject << /Im1 6 0 R >>"
    objects[2] = (
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] {page_extra} "
        f"/Resources << {resources} >> /Contents 4 0 R >>"
    ).encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def _font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=px)


def _draw_lines(dpi: int) -> Image.Image:
    """The LINES drawn as pixels on a white Letter page at ``dpi``."""
    scale = dpi / 72
    img = Image.new("L", (round(612 * scale), round(792 * scale)), 255)
    draw = ImageDraw.Draw(img)
    for x, y, text, size in LINES:
        px = round(size * scale)
        # PDF y is the baseline from the bottom; Pillow's is the top from the top.
        draw.text((x * scale, (792 - y) * scale - px * 0.8), text, fill=0, font=_font(px))
    return img


def rotated() -> bytes:
    return _pdf(_text_stream(LINES), page_extra="/Rotate 90")


def cropped() -> bytes:
    outside = [(72, 60, "OUTSIDECROP", 10)]  # y=60 is below the cropbox's bottom edge (120)
    return _pdf(_text_stream(LINES + outside), page_extra="/CropBox [40 120 560 760]")


def image_only() -> bytes:
    img = _draw_lines(150)
    draw_full_page = "q 612 0 0 792 0 0 cm /Im1 Do Q".encode()
    return _pdf(draw_full_page, image=(img.width, img.height, img.tobytes()))


# The value image on the handwritten form: where it sits, in PDF points.
FORM_VALUE_BOX = (252, 548, 300, 566)  # x0, y0 (bottom), x1, y1 (top)


def form_handwritten() -> bytes:
    printed = [line for line in LINES if not line[2].startswith(("Hemoglobin", "Glucose", "Creatinine"))]
    printed += [
        (72, 558, "Hemoglobin A1c", 10),
        (324, 558, "%         4.0-5.6", 10),
    ]
    x0, y0, x1, y1 = FORM_VALUE_BOX
    scale = 200 / 72
    img = Image.new("L", (round((x1 - x0) * scale), round((y1 - y0) * scale)), 255)
    ImageDraw.Draw(img).text((6, 2), "7.4", fill=0, font=_font(round(14 * scale)))
    content = _text_stream(printed) + f"\nq {x1 - x0} 0 0 {y1 - y0} {x0} {y0} cm /Im1 Do Q".encode()
    return _pdf(content, image=(img.width, img.height, img.tobytes()))


def photo_exif() -> bytes:
    """Stored rotated 90 degrees counter-clockwise; EXIF 6 tells a viewer to turn it back."""
    upright = _draw_lines(100).crop((0, 0, 850, 560))
    stored = upright.rotate(90, expand=True)
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    stored.save(buf, format="JPEG", quality=92, exif=exif.tobytes())
    return buf.getvalue()


FIXTURES = {
    "v_rotated.pdf": rotated,
    "v_cropped.pdf": cropped,
    "v_image_only.pdf": image_only,
    "v_form_handwritten.pdf": form_handwritten,
    "v_photo_exif.jpg": photo_exif,
}

if __name__ == "__main__":
    for name, build in FIXTURES.items():
        path = HERE / name
        path.write_bytes(build())
        print(f"wrote {path.name} ({path.stat().st_size} bytes)")
