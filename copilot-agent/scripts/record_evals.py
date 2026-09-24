#!/usr/bin/env python
"""Record real model responses for the eval cases that exercise the model.

    uv run python scripts/record_evals.py          # record all
    uv run python scripts/record_evals.py --list    # show what exists

Costs tokens. Run deliberately, never in CI. The recordings it writes are
committed, and every later run replays them for free — see app/recording.py
for why staleness is fatal rather than auto-refreshed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.lab_extractor import extract_lab_document  # noqa: E402
from app.providers.anthropic_provider import AnthropicProvider  # noqa: E402
from app.recording import RECORDINGS_DIR, RecordingProvider, recorded_case_ids  # noqa: E402
from app.settings import ModelSettings  # noqa: E402

FIXTURES = REPO / "fixtures" / "documents"

# Each case pairs an id with the synthetic document it reads. Both exercise a
# behaviour that scripted output cannot: the clean one that no printed flag is
# invented, the degraded one that an obscured value is refused rather than guessed.
CASES: list[tuple[str, Path, int]] = [
    ("lab_clean_hba1c", FIXTURES / "lab_hba1c_clean.pdf", 101),
    ("lab_degraded_scan", FIXTURES / "lab_hba1c_degraded_scan.pdf", 102),
]


async def record_one(case_id: str, pdf: Path, document_id: int) -> None:
    settings = ModelSettings()
    provider = RecordingProvider(AnthropicProvider(settings), case_id)
    doc = await extract_lab_document(
        document_id=document_id,
        pdf_bytes=pdf.read_bytes(),
        media_type="application/pdf",
        provider=provider,
    )
    results = ", ".join(
        f"{r.test_name}={r.value if r.value is not None else 'UNREADABLE'}" for r in doc.results
    ) or "(none)"
    print(f"  {case_id}: {len(doc.results)} results — {results}")


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="show existing recordings and exit")
    args = parser.parse_args(argv)

    if args.list:
        ids = recorded_case_ids()
        print(f"{len(ids)} recording(s) in {RECORDINGS_DIR.relative_to(REPO)}:")
        for i in ids:
            print(f"  {i}")
        return 0

    print(f"Recording {len(CASES)} case(s) against the live model. This costs tokens.\n")
    for case_id, pdf, document_id in CASES:
        if not pdf.exists():
            print(f"  {case_id}: SKIPPED — {pdf.name} not found", file=sys.stderr)
            continue
        await record_one(case_id, pdf, document_id)
    print(f"\nWritten to {RECORDINGS_DIR.relative_to(REPO)}. Commit them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
