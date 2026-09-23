"""Lab-PDF extraction pipeline (PRD CR2, feature PRD F03).

    document bytes
        -> DocumentPart (base64, never logged)
        -> ModelProvider.parse_structured(schema=LabDocument)
        -> LabDocument (schema-validated, still untrusted)
        -> deterministic post-processing: application-owned identity,
           application-derived abnormal flags, recomputed metadata
        -> LabDocument (public contract)

Three things this module refuses to do, each because the alternative is a
patient-safety defect rather than a bug:

1. **It never lets the model derive an abnormal flag.** ``abnormal_flag_source``
   distinguishes a flag *printed on the report* from one *we computed*. The
   prompt forbids the model from comparing value to range; the comparison is
   made here, deterministically, and is always labelled ``derived`` with the
   range it used. A computed comparison shown as a lab-printed flag is the most
   consequential display error in this system (W2-AMB-055).

2. **It never repairs an unreadable value.** A result the model marks
   ``unreadable`` reaches the caller with ``value=None``; the ``LabResult``
   invariant makes any other outcome a validation failure at the provider
   boundary rather than a guess filed as fact (W2-AMB-010).

3. **It never logs the document.** The bytes and their base64 encoding stay in
   local variables and the provider call. Every log line here carries counts,
   ids and timings only (CR7).

No PDF parsing library is involved: the provider reads the document natively.
``StubProvider`` is the default so the pipeline runs offline, with no API key
and no spend; it is registered below through ``register_fixture`` so neither the
``ModelProvider`` port nor ``StubProvider`` itself grows a member per document
type.
"""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.documents import (
    AbnormalFlag,
    AbnormalFlagSource,
    DocumentCitation,
    ExtractionMetadata,
    LabDocument,
    LabResult,
    VerificationStatus,
)
from app.observability import generation, log_event
from app.providers.base import (
    ContentPart,
    DocumentPart,
    ModelProvider,
    ProviderConfigurationError,
    TextPart,
)
from app.providers.prompt import (
    LAB_EXTRACTION_PROMPT_VERSION,
    LAB_EXTRACTION_SYSTEM_PROMPT,
    build_lab_document_content,
)
from app.providers.stub_provider import StubProvider

# A one-page report with a dozen results fits comfortably; the ceiling exists so
# a malformed document cannot turn into an unbounded generation.
LAB_MAX_OUTPUT_TOKENS = 4096

# What a vision model can actually read. Sending anything else is a wasted paid
# call, so it is refused here rather than at the vendor.
SUPPORTED_MEDIA_TYPES = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp"}
)


# --------------------------------------------------------------------------- #
# Deterministic post-processing
# --------------------------------------------------------------------------- #

# Only the plain "low-high" form is understood. Anything else - "<5.7", "Negative",
# "see report" - yields no bounds, and no flag is derived. Guessing the semantics
# of a range we do not recognise would produce exactly the wrong kind of confidence.
_NUMERIC_RANGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*$")


def parse_reference_range(printed: str | None) -> tuple[Decimal, Decimal] | None:
    """Bounds of a printed ``low-high`` range, or None when it is not that shape."""
    if printed is None:
        return None
    match = _NUMERIC_RANGE.match(printed)
    if match is None:
        return None
    try:
        low, high = Decimal(match.group(1)), Decimal(match.group(2))
    except InvalidOperation:  # pragma: no cover - the regex already constrains this
        return None
    return (low, high) if low <= high else None


def derive_abnormal_flag(value: object, reference_range: str | None) -> AbnormalFlag | None:
    """Compare a numeric value to its printed range. None when the comparison cannot be made.

    This is *our* comparison, never the lab's. Callers must record the result as
    ``AbnormalFlagSource.DERIVED``.
    """
    if not isinstance(value, Decimal):
        return None
    bounds = parse_reference_range(reference_range)
    if bounds is None:
        return None
    low, high = bounds
    if value < low:
        return AbnormalFlag.LOW
    if value > high:
        return AbnormalFlag.HIGH
    return AbnormalFlag.NORMAL


def _revalidated(result: LabResult, **changes: Any) -> LabResult:
    """Apply ``changes`` through validation, so no invariant is bypassed.

    ``model_copy(update=...)`` would skip the ``LabResult`` validators - the very
    checks that stop a derived flag travelling without its range.
    """
    return LabResult.model_validate({**result.model_dump(), **changes})


def apply_derived_flags(results: list[LabResult]) -> list[LabResult]:
    """Fill in the abnormality the report did not print, labelled as ours.

    A flag the model read off the page (``extracted``) is left exactly as it is.
    """
    out: list[LabResult] = []
    for result in results:
        if result.abnormal_flag is not None or result.abnormal_flag_source is not AbnormalFlagSource.UNAVAILABLE:
            out.append(result)
            continue
        flag = derive_abnormal_flag(result.value, result.reference_range)
        if flag is None:
            out.append(result)
            continue
        out.append(
            _revalidated(result, abnormal_flag=flag, abnormal_flag_source=AbnormalFlagSource.DERIVED)
        )
    return out


def stamp_source_identity(results: list[LabResult], document_id: int) -> list[LabResult]:
    """Re-assign every citation's ``source_id`` from the argument, not the model.

    The model never owns identity (``providers/base.py``): a document id echoed
    back wrongly would attribute an extraction to the wrong stored file.
    """
    return [
        _revalidated(result, citation=result.citation.model_copy(update={"source_id": str(document_id)}).model_dump())
        for result in results
    ]


def summarize(results: list[LabResult], *, model_id: str, page_count: int) -> ExtractionMetadata:
    """Metadata recomputed from the results, so the counts cannot disagree with them."""
    verified = sum(
        1
        for r in results
        if r.verification_status in (VerificationStatus.VERIFIED_EXACT, VerificationStatus.VERIFIED_FUZZY)
    )
    return ExtractionMetadata(
        model_id=model_id,
        prompt_version=LAB_EXTRACTION_PROMPT_VERSION,
        extracted_at=datetime.now(UTC),
        page_count=max(1, page_count),
        verified_fraction=(verified / len(results)) if results else 0.0,
        unreadable_count=sum(1 for r in results if r.verification_status is VerificationStatus.UNREADABLE),
        unverified_count=sum(1 for r in results if r.verification_status is VerificationStatus.UNVERIFIED),
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def extract_lab_document(
    *,
    document_id: int,
    pdf_bytes: bytes,
    media_type: str,
    provider: ModelProvider | None = None,
) -> LabDocument:
    """Extract one stored lab report into a validated ``LabDocument``.

    ``provider`` defaults to ``StubProvider``: deterministic, offline, no API key
    and no spend. Raises ``ProviderError`` when the model call fails - a provider
    outage must never arrive as an empty extraction.
    """
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ValueError("unsupported document media type")
    if not pdf_bytes:
        raise ValueError("document is empty")

    model = provider if provider is not None else StubProvider()

    # The only place the bytes are encoded, and the only place the encoding goes.
    content: list[ContentPart] = [
        TextPart(text=build_lab_document_content(document_id)),
        DocumentPart(media_type=media_type, data_base64=base64.b64encode(pdf_bytes).decode("ascii")),
    ]

    log_event(
        "lab_extraction.start",
        document_id=document_id,
        media_type=media_type,
        byte_count=len(pdf_bytes),  # size only: never the bytes, never the base64
        provider=model.name,
    )

    with generation("lab_extract") as gen:
        # input/output are deliberately never set: they would be the document.
        parsed = await model.parse_structured(
            system=LAB_EXTRACTION_SYSTEM_PROMPT,
            content=content,
            schema=LabDocument,
            max_tokens=LAB_MAX_OUTPUT_TOKENS,
        )
        gen["usage"] = parsed.usage

    proposed = parsed.output
    results = apply_derived_flags(stamp_source_identity(list(proposed.results), document_id))
    document = LabDocument(
        document_id=document_id,  # assigned here, never taken from the model
        collection_date=proposed.collection_date,
        ordering_provider=proposed.ordering_provider,
        results=results,
        extraction_metadata=summarize(
            results,
            model_id=parsed.usage.model,
            page_count=proposed.extraction_metadata.page_count,
        ),
    )

    log_event(
        "lab_extraction.complete",
        document_id=document_id,
        provider=parsed.usage.provider,
        model=parsed.usage.model,
        latency_ms=parsed.usage.latency_ms,
        result_count=len(document.results),
        unreadable_count=document.extraction_metadata.unreadable_count,
        unverified_count=document.extraction_metadata.unverified_count,
        derived_flag_count=sum(
            1 for r in document.results if r.abnormal_flag_source is AbnormalFlagSource.DERIVED
        ),
        # No test name, no value, no unit, no range, no patient identifier.
    )
    return document


# --------------------------------------------------------------------------- #
# StubProvider fixture
#
# Registration, not inheritance: the port keeps four members and StubProvider
# keeps one parse path (W2_ARCHITECTURE_DECISIONS.md, "Design principles").
#
# The fixture reads the printed text out of the supplied document rather than
# returning a canned answer per file, so it exercises the same two failure modes
# the real model must handle - a legible value and an obscured one - and a new
# fixture PDF needs no change here.
# --------------------------------------------------------------------------- #

_PDF_SHOW_TEXT = re.compile(rb"\(((?:[^()\\]|\\.)*)\)\s*Tj")
_PDF_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
_RESULT_ROW = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9 ,.'/-]*?)\s{2,}"
    r"(?P<value>[0-9#][0-9#.]*)\s+"
    r"(?P<unit>\S+)\s+"
    r"(?P<ref>\S+)\s*$"
)
_COLLECTED = re.compile(r"COLLECTED:\s*(\d{4}-\d{2}-\d{2})")
_ORDERING_PROVIDER = re.compile(r"ORDERING PROVIDER:\s*(.+?)\s*$")
_DOCUMENT_ID = re.compile(r"<document_id>(\d+)</document_id>")
_LEGIBLE_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")


def _printed_lines(pdf_bytes: bytes) -> list[str]:
    """Text-showing operands of a simple, uncompressed single-page PDF."""
    return [
        m.group(1).decode("latin-1").replace("\\(", "(").replace("\\)", ")")
        for m in _PDF_SHOW_TEXT.finditer(pdf_bytes)
    ]


def _stub_lab_document(content: list[ContentPart]) -> LabDocument:
    """Deterministic ``LabDocument`` read off the supplied fixture document."""
    documents = [p for p in content if isinstance(p, DocumentPart)]
    if not documents:
        raise ProviderConfigurationError("lab extraction requires a document part")
    raw = base64.b64decode(documents[0].data_base64)
    lines = _printed_lines(raw)

    text = "\n".join(p.text for p in content if isinstance(p, TextPart))
    id_match = _DOCUMENT_ID.search(text)
    # Placeholder only: extract_lab_document re-stamps every citation from its argument.
    source_id = id_match.group(1) if id_match else "0"

    collection_date = None
    ordering_provider = None
    results: list[LabResult] = []

    for line in lines:
        if collection_date is None and (m := _COLLECTED.search(line)):
            collection_date = m.group(1)
        if ordering_provider is None and (m := _ORDERING_PROVIDER.search(line)):
            ordering_provider = m.group(1)

        row = _RESULT_ROW.match(line)
        if row is None:
            continue
        printed_value = row.group("value")
        legible = _LEGIBLE_NUMBER.match(printed_value) is not None
        results.append(
            LabResult(
                test_name=row.group("name").strip(),
                # An obscured value is named unreadable, never reconstructed from
                # the range or from the other results.
                value=Decimal(printed_value) if legible else None,
                unit=row.group("unit"),
                reference_range=row.group("ref"),
                collection_date=collection_date,
                # This report prints no flag column; the derivation is the caller's.
                abnormal_flag=None,
                abnormal_flag_source=AbnormalFlagSource.UNAVAILABLE,
                verification_status=(
                    VerificationStatus.VERIFIED_EXACT if legible else VerificationStatus.UNREADABLE
                ),
                citation=DocumentCitation(
                    source_id=source_id,
                    page_or_section="p. 1",
                    field_or_chunk_id=f"results[{len(results)}].value",
                    quote_or_value=printed_value,  # exactly as printed, "8.#" included
                ),
            )
        )

    page_count = max(1, len(_PDF_PAGE_OBJECT.findall(raw)))
    return LabDocument(
        document_id=int(source_id),
        collection_date=collection_date,
        ordering_provider=ordering_provider,
        results=results,
        extraction_metadata=summarize(results, model_id="stub-deterministic", page_count=page_count),
    )


StubProvider.register_fixture(LabDocument, _stub_lab_document)


__all__ = [
    "LAB_MAX_OUTPUT_TOKENS",
    "SUPPORTED_MEDIA_TYPES",
    "apply_derived_flags",
    "derive_abnormal_flag",
    "extract_lab_document",
    "parse_reference_range",
    "stamp_source_identity",
    "summarize",
]
