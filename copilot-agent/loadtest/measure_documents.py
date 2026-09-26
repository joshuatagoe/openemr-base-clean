"""Week 2 cost & latency measurement: the real document pipeline over the synthetic fixtures.

Runs each fixture document through the two routes the module calls - extract (``/v1/documents/extract``
logic) then the stored-document briefing - with the configured provider, OCR and reranker, and records
per-stage wall-clock, Textract pages and rerank calls. Model tokens and cost come from the Langfuse
generations of the same run (environment ``measurement-w2``), queried afterwards.

Spends real money when run with MODEL_PROVIDER=anthropic, COPILOT_OCR=textract and
COPILOT_RERANKER=bedrock (about $0.05 per document). Synthetic documents only. Output is numbers only:
no document text, no values, no identities.

    uv run --env-file .env python -m loadtest.measure_documents --out loadtest/W2_MEASUREMENT.json
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

import app.page_text as page_text
import app.reranker as reranker_module
from app.document_briefing import (
    DocumentBriefingRequest,
    DocumentExtractRequest,
    StoredDocument,
    build_reranker,
    run_document_briefing,
    run_document_extract,
)
from app.main import build_provider
from app.observability import configure_tracing, shutdown_tracing
from app.settings import ModelSettings, ServiceSettings, TracingSettings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "documents"
ENVIRONMENT = "measurement-w2"
MEDIA = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def fixture_documents(which: str) -> list[tuple[Path, str]]:
    """(path, doc_type). ``text``: the text-layer lab set (top level + generated/). ``scans``: the
    documents that need OCR or a photo path (starter/, verification/, demo/). ``intake``: intake forms."""
    def labs(folder: Path) -> list[tuple[Path, str]]:
        return [(p, "lab_pdf") for p in sorted(folder.glob("*")) if p.suffix.lower() in MEDIA]

    if which == "text":
        return labs(FIXTURES) + labs(FIXTURES / "generated")
    if which == "scans":
        return labs(FIXTURES / "starter") + labs(FIXTURES / "verification") + labs(FIXTURES / "demo")
    return [(p, "intake_form") for p in sorted((FIXTURES / "intake").glob("*")) if p.suffix.lower() in MEDIA]


class Counter:
    """Counts and times the external calls the traces do not price: Textract pages and rerank requests."""

    def __init__(self) -> None:
        self.ocr_pages = 0
        self.ocr_seconds: list[float] = []
        self.rerank_calls = 0
        self.rerank_seconds: list[float] = []

    def install(self) -> None:
        read_page = page_text.TextractOcr.read_page
        call = reranker_module.BedrockReranker._call
        counter = self

        async def timed_read_page(self: Any, png: bytes, *, page: int) -> Any:
            start = time.perf_counter()
            try:
                return await read_page(self, png, page=page)
            finally:
                counter.ocr_pages += 1
                counter.ocr_seconds.append(time.perf_counter() - start)

        def timed_call(self: Any, *args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return call(self, *args, **kwargs)
            finally:
                counter.rerank_calls += 1
                counter.rerank_seconds.append(time.perf_counter() - start)

        page_text.TextractOcr.read_page = timed_read_page  # type: ignore[method-assign]
        reranker_module.BedrockReranker._call = timed_call  # type: ignore[method-assign]


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 3)


def summary(values: list[float]) -> dict[str, Any]:
    return {"n": len(values), "p50_s": pct(values, 0.5), "p95_s": pct(values, 0.95),
            "mean_s": round(statistics.mean(values), 3) if values else None, "max_s": round(max(values), 3) if values else None}


async def measure(which: str, limit: int | None) -> dict[str, Any]:
    service = ServiceSettings()
    model_settings = ModelSettings()
    tracing = TracingSettings()
    configure_tracing(
        enabled=tracing.is_enabled(),
        public_key=tracing.langfuse_public_key.get_secret_value() if tracing.langfuse_public_key else None,
        secret_key=tracing.langfuse_secret_key.get_secret_value() if tracing.langfuse_secret_key else None,
        base_url=tracing.langfuse_base_url,
        environment=ENVIRONMENT,
    )
    provider = build_provider(model_settings)
    reranker = build_reranker(service.reranker, region=service.bedrock_region)
    counter = Counter()
    counter.install()

    rows: list[dict[str, Any]] = []
    for path, doc_type in fixture_documents(which)[:limit]:
        raw = path.read_bytes()
        patient = uuid.uuid4()
        pages_before, rerank_before = counter.ocr_pages, counter.rerank_calls
        start = time.perf_counter()
        extracted = await run_document_extract(
            DocumentExtractRequest(
                correlation_id=uuid.uuid4(), patient_uuid=patient, document_id=len(rows) + 1, doc_type=doc_type,
                media_type=MEDIA[path.suffix.lower()], document_base64=base64.b64encode(raw).decode(),
            ),
            provider=provider,
        )
        extract_s = time.perf_counter() - start
        briefing_s = None
        briefing_status = None
        if extracted.status == "ok" and extracted.extraction is not None:
            start = time.perf_counter()
            briefing = await run_document_briefing(
                DocumentBriefingRequest(
                    correlation_id=uuid.uuid4(), patient_uuid=patient,
                    documents=[StoredDocument(document_id=len(rows) + 1, doc_type=doc_type,
                                              extraction=extracted.extraction.model_dump(mode="json"))],
                ),
                provider=provider, reranker=reranker,
            )
            briefing_s = time.perf_counter() - start
            briefing_status = briefing.status.value
        rows.append({
            "fixture": path.name, "doc_type": doc_type, "bytes": len(raw),
            "extract_status": extracted.status, "extract_reason": extracted.degraded_reason,
            "extract_s": round(extract_s, 3), "ocr_pages": counter.ocr_pages - pages_before,
            "briefing_status": briefing_status, "briefing_s": round(briefing_s, 3) if briefing_s else None,
            "rerank_calls": counter.rerank_calls - rerank_before,
        })
        print(json.dumps(rows[-1]), flush=True)

    shutdown_tracing()
    extract = [r["extract_s"] for r in rows]
    brief = [r["briefing_s"] for r in rows if r["briefing_s"]]
    return {
        "environment": ENVIRONMENT, "set": which, "provider": model_settings.model_provider, "ocr": service.ocr,
        "reranker": service.reranker, "documents": len(rows),
        "extract": summary(extract), "briefing": summary(brief),
        "extract_plus_briefing": summary([r["extract_s"] + (r["briefing_s"] or 0) for r in rows]),
        "ocr_page": summary(counter.ocr_seconds), "ocr_pages_total": counter.ocr_pages,
        "rerank_call": summary(counter.rerank_seconds), "rerank_calls_total": counter.rerank_calls,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--set", dest="which", choices=("text", "scans", "intake"), default="text")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    result = asyncio.run(measure(args.which, args.limit))
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
