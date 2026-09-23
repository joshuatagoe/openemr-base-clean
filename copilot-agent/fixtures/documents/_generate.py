"""Generate synthetic lab-PDF fixtures. Run: uv run python fixtures/documents/_generate.py

No PDF library. A single-page text PDF is about forty lines of structure, and
writing it directly buys three things that matter for a CI gate: the bytes are
deterministic (no library version drift changing the digest the eval baseline
records), there is no new runtime dependency, and nothing unsigned gets
installed - which on this machine means nothing new for Smart App Control to
block mid-pipeline.

All content is synthetic. The scenario matches ADR-006 section 9.3: an HbA1c of
8.9% against a printed range of 4.0-5.6, carrying NO printed abnormal flag. That
absence is the point. The system must derive the comparison itself and label it
as derived, never presenting a computed abnormality as a lab-printed flag
(W2-AMB-055).
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent


def _pdf(lines: list[tuple[int, int, str, int]]) -> bytes:
    """Build a one-page US-Letter PDF. Each line is (x, y, text, font_size), y from bottom."""
    parts = [f"BT /F1 {size} Tf {x} {y} Td ({text.replace('(', '[').replace(')', ']')}) Tj ET"
             for x, y, text, size in lines]
    stream = "\n".join(parts).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n".encode()
        + b"%%EOF\n"
    )
    return bytes(out)


# The clean case: every field CR2 requires is legibly present.
CLEAN = [
    (72, 720, "NORTHSIDE CLINICAL LABORATORY", 14),
    (72, 700, "440 Cedar Street, Austin TX 78701  CLIA 45D2109876", 8),
    (72, 664, "PATIENT: Whitfield, Evelyn R.        DOB: 1981-03-14", 10),
    (72, 650, "MRN: 7          SEX: F", 10),
    (72, 636, "ORDERING PROVIDER: Marcus Adeyemi, MD", 10),
    (72, 622, "COLLECTED: 2026-09-12 08:15        REPORTED: 2026-09-13 06:40", 10),
    (72, 588, "TEST                          RESULT      UNITS     REFERENCE RANGE", 10),
    (72, 574, "--------------------------------------------------------------------", 10),
    (72, 558, "Hemoglobin A1c                8.9         %         4.0-5.6", 10),
    (72, 542, "Glucose, Fasting              164         mg/dL     70-99", 10),
    (72, 526, "Creatinine                    0.9         mg/dL     0.6-1.1", 10),
    (72, 494, "No abnormal flags printed on this report.", 9),
]

# The degraded case: one value is obscured, as a poor scan would leave it.
# The system must name it unreadable, never guess it (F03, W2-AMB-010).
DEGRADED = CLEAN[:8] + [
    (72, 558, "Hemoglobin A1c                8.#         %         4.0-5.6", 10),
    (72, 542, "Glucose, Fasting              1##         mg/dL     70-99", 10),
    (72, 526, "Creatinine                    0.9         mg/dL     0.6-1.1", 10),
    (72, 494, "Scan quality degraded; two results partially obscured.", 9),
]

FIXTURES = {"lab_hba1c_clean.pdf": CLEAN, "lab_hba1c_degraded_scan.pdf": DEGRADED}

if __name__ == "__main__":
    for name, lines in FIXTURES.items():
        path = HERE / name
        path.write_bytes(_pdf(lines))
        print(f"wrote {path.name} ({path.stat().st_size} bytes)")
