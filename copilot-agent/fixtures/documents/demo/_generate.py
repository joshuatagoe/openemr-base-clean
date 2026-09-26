"""Generate the demo documents. Run: uv run python fixtures/documents/demo/_generate.py

All content is synthetic. Each file prints the seeded demo patient exactly as the chart holds her
(dev/seed_evelyn_demo.php: Demo, Evelyn, DOB 1958-04-12, female), so the identity check passes and
the briefing can compare against her chart: HbA1c 8.9 % on 2026-09-12 and a lipid panel ordered
2026-09-14 with no result yet. The one exception is ``demo_wrong_patient.pdf``, which prints a
different patient on purpose, to show the identity hold. See README.md for what each shows.

PDFs are written by hand, as in ../verification/_generate.py, so the bytes are deterministic.
"""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent

_spec = importlib.util.spec_from_file_location("verification_generate", HERE.parent / "verification" / "_generate.py")
assert _spec is not None and _spec.loader is not None
_v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_v)

HEADER = [
    (72, 720, "NORTHSIDE CLINICAL LABORATORY", 14),
    (72, 700, "440 Cedar Street, Austin TX 78701  CLIA 45D2109876", 8),
    (72, 664, "PATIENT: Demo, Evelyn               DOB: 1958-04-12", 10),
    (72, 650, "SEX: F", 10),
    (72, 636, "ORDERING PROVIDER: Marcus Adeyemi, MD", 10),
]
COLUMNS = [
    (72, 588, "TEST                          RESULT      UNITS     REFERENCE RANGE   FLAG", 10),
    (72, 574, "------------------------------------------------------------------------------", 10),
]

# The follow-up the chart is waiting for: HbA1c repeated (8.9 -> 8.1, no printed flag, so the
# comparison is computed and labelled as such) and the pending lipid panel resulted, with the
# lab's own H flags printed.
FOLLOWUP = HEADER + [(72, 622, "COLLECTED: 2026-09-24 08:05        REPORTED: 2026-09-24 16:20", 10)] + COLUMNS + [
    (72, 558, "Hemoglobin A1c                8.1         %         4.0-5.6", 10),
    (72, 542, "Glucose, Fasting              152         mg/dL     70-99             H", 10),
    (72, 526, "Cholesterol, Total            214         mg/dL     <200              H", 10),
    (72, 510, "LDL Cholesterol               131         mg/dL     <100              H", 10),
    (72, 494, "HDL Cholesterol               46          mg/dL     >40", 10),
    (72, 478, "Triglycerides                 185         mg/dL     <150              H", 10),
    (72, 462, "Creatinine                    0.9         mg/dL     0.6-1.1", 10),
    (72, 430, "H = above reference range. Flags are printed by the laboratory.", 8),
]

# A scanned page: no text layer, slightly skewed, speckled. Only OCR can read it.
SCAN = HEADER + [(72, 622, "COLLECTED: 2026-09-25 07:50        REPORTED: 2026-09-25 13:10", 10)] + COLUMNS + [
    (72, 558, "Sodium                        139         mmol/L    136-145", 10),
    (72, 542, "Potassium                     5.4         mmol/L    3.5-5.1           H", 10),
    (72, 526, "Chloride                      103         mmol/L    98-107", 10),
    (72, 510, "Bicarbonate                   24          mmol/L    22-29", 10),
    (72, 494, "BUN                           18          mg/dL     7-20", 10),
    (72, 478, "Creatinine                    1.0         mg/dL     0.6-1.1", 10),
]

# The same follow-up printed for someone else: the identity check must hold it.
WRONG_PATIENT = [
    (x, y, "PATIENT: Demo, Eleanor              DOB: 1962-11-03", s) if t.startswith("PATIENT:") else (x, y, t, s)
    for x, y, t, s in FOLLOWUP
]


def _text_pdf(lines: list[tuple[int, int, str, int]]) -> bytes:
    return _v._pdf(_v._text_stream(lines))


def _scan_pdf(lines: list[tuple[int, int, str, int]]) -> bytes:
    dpi = 150
    scale = dpi / 72
    img = Image.new("L", (round(612 * scale), round(792 * scale)), 250)
    draw = ImageDraw.Draw(img)
    for x, y, text, size in lines:
        px = round(size * scale)
        draw.text((x * scale, (792 - y) * scale - px * 0.8), text, fill=20, font=_v._font(px))
    rng = random.Random(7)  # deterministic speckle
    for _ in range(4000):
        img.putpixel((rng.randrange(img.width), rng.randrange(img.height)), rng.choice((170, 200, 230)))
    img = img.rotate(-0.8, resample=Image.BICUBIC, fillcolor=250)
    return _v._pdf(b"q 612 0 0 792 0 0 cm /Im1 Do Q", image=(img.width, img.height, img.tobytes()))


FIXTURES = {
    "demo_lab_followup.pdf": lambda: _text_pdf(FOLLOWUP),
    "demo_lab_scan.pdf": lambda: _scan_pdf(SCAN),
    "demo_wrong_patient.pdf": lambda: _text_pdf(WRONG_PATIENT),
}

if __name__ == "__main__":
    for name, build in FIXTURES.items():
        path = HERE / name
        path.write_bytes(build())
        print(f"wrote {path.name} ({path.stat().st_size} bytes)")
