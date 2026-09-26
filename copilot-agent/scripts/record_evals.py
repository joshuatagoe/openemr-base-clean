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
def _cases() -> list[dict]:
    """Every document case in fixtures/doc_cases/ - the case file names its document and id."""
    from app.doc_eval import load_doc_cases

    return load_doc_cases()


def _document(case: dict) -> Path:
    return FIXTURES / (case.get("document") or case["pdf"])


CASES = _cases()


class _CapturingOcr:
    """Wraps the real Textract source and keeps the words it returned, to commit as a fixture."""

    name = "recording-ocr"

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.words: list[dict] = []

    async def read_page(self, png: bytes, *, page: int):  # noqa: ANN201 - OcrSource
        words = await self._inner.read_page(png, page=page)  # type: ignore[attr-defined]
        self.words += [{"text": w.text, "page": w.page, "bbox": list(w.bbox)} for w in words]
        return words


async def record_intake(case: dict) -> None:
    """Intake case: the real model, and for a photo the real Textract words (ADR-007, us-east-2)."""
    import hashlib
    import json

    from app.doc_eval import OCR_RECORDINGS_DIR
    from app.intake import intake_items
    from app.intake_extractor import extract_intake_document
    from app.page_text import FakeOcr, TextractOcr
    from app.settings import ServiceSettings

    data = _document(case).read_bytes()
    ocr = FakeOcr()
    if case.get("ocr") == "recorded":
        ocr = _CapturingOcr(TextractOcr(region=ServiceSettings().textract_region))
    provider = RecordingProvider(AnthropicProvider(ModelSettings()), case["case_id"])
    form = await extract_intake_document(
        document_id=case["document_id"], document_bytes=data, media_type=case["media_type"], provider=provider, ocr=ocr
    )
    if isinstance(ocr, _CapturingOcr):
        OCR_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        (OCR_RECORDINGS_DIR / f"{case['case_id']}.json").write_text(
            json.dumps(
                {"case_id": case["case_id"], "document_sha256": hashlib.sha256(data).hexdigest(), "words": ocr.words},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    # Synthetic fixtures only, so the items may be printed for the reviewer.
    items = "; ".join(f"{i.label}={i.text} [{i.verification_status.value}]" for i in intake_items(form))
    print(f"  {case['case_id']}: {items or '(none)'}")


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
    parser.add_argument("--missing", action="store_true", help="record only cases with no recording yet")
    args = parser.parse_args(argv)

    if args.list:
        ids = recorded_case_ids()
        print(f"{len(ids)} recording(s) in {RECORDINGS_DIR.relative_to(REPO)}:")
        for i in ids:
            print(f"  {i}")
        return 0

    done = set(recorded_case_ids()) if args.missing else set()
    todo = [c for c in CASES if c["case_id"] not in done]
    print(f"Recording {len(todo)} case(s) against the live model. This costs tokens.\n")
    for case in todo:
        path = _document(case)
        if not path.exists():
            print(f"  {case['case_id']}: SKIPPED — {path.name} not found", file=sys.stderr)
            continue
        if case.get("kind") == "intake":
            await record_intake(case)
        else:
            await record_one(case["case_id"], path, case["document_id"])
    print(f"\nWritten to {RECORDINGS_DIR.relative_to(REPO)}. Commit them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
