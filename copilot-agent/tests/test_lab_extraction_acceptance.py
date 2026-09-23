"""Acceptance tests for lab-PDF ingestion, written from F03 before the feature exists.

Two groups, and the split is the point:

The SCHEMA tests pass today. They pin the contract F03 specifies, so an
implementer cannot quietly widen it - which is the failure mode that makes a
"passing" extractor meaningless.

The BEHAVIOUR tests fail today, deliberately, with ImportError on a module that
does not exist yet. That is the expected failure: it says the contract is agreed
and the behaviour is owed. A test that failed on a missing fixture or a broken
import path would prove nothing.

Nothing here is a fixture-integrity check. Every assertion is about what the
system must do with a document.
"""

from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.documents import (
    AbnormalFlag,
    AbnormalFlagSource,
    DocumentCitation,
    ExtractionMetadata,
    LabDocument,
    LabResult,
    VerificationStatus,
)

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"
CLEAN = FIXTURES / "lab_hba1c_clean.pdf"
DEGRADED = FIXTURES / "lab_hba1c_degraded_scan.pdf"


def _citation(**over: object) -> DocumentCitation:
    base = dict(
        source_id="101",
        page_or_section="p. 1",
        field_or_chunk_id="results[0].value",
        quote_or_value="8.9",
    )
    return DocumentCitation(**{**base, **over})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Contract: these hold now and must keep holding
# --------------------------------------------------------------------------- #


def test_a_numeric_result_without_a_unit_is_rejected() -> None:
    """CR2 requires unit alongside value. '8.9' with no unit is not a lab result."""
    with pytest.raises(ValidationError):
        LabResult(test_name="Hemoglobin A1c", value=Decimal("8.9"), citation=_citation())


def test_a_derived_flag_without_a_reference_range_is_rejected() -> None:
    """A computed abnormality must carry the range it was computed against, or it is an assertion."""
    with pytest.raises(ValidationError):
        LabResult(
            test_name="Hemoglobin A1c",
            value=Decimal("8.9"),
            unit="%",
            abnormal_flag=AbnormalFlag.HIGH,
            abnormal_flag_source=AbnormalFlagSource.DERIVED,
            reference_range=None,
            citation=_citation(),
        )


def test_a_flag_must_declare_whether_it_was_printed_or_derived() -> None:
    """The most consequential display error in this system is a computed flag shown as a printed one."""
    with pytest.raises(ValidationError):
        LabResult(
            test_name="Hemoglobin A1c",
            value=Decimal("8.9"),
            unit="%",
            reference_range="4.0-5.6",
            abnormal_flag=AbnormalFlag.HIGH,
            abnormal_flag_source=AbnormalFlagSource.UNAVAILABLE,
            citation=_citation(),
        )


def test_an_unreadable_result_must_not_carry_a_value() -> None:
    """Unreadable means unreadable. A guessed value filed as fact is the failure F03 exists to prevent."""
    with pytest.raises(ValidationError):
        LabResult(
            test_name="Hemoglobin A1c",
            value=Decimal("8.9"),
            unit="%",
            verification_status=VerificationStatus.UNREADABLE,
            citation=_citation(),
        )


def test_an_invented_field_is_rejected() -> None:
    """extra='forbid': a model that hallucinates a field fails validation, not review."""
    with pytest.raises(ValidationError):
        LabResult(
            test_name="Hemoglobin A1c",
            value=Decimal("8.9"),
            unit="%",
            citation=_citation(),
            confidence=0.97,  # type: ignore[call-arg]
        )


def test_a_bbox_outside_the_page_is_rejected_not_clamped() -> None:
    """Silently corrected coordinates draw a highlight over the wrong text - worse than no highlight."""
    with pytest.raises(ValidationError):
        _citation(page=1, bbox=(0.1, 0.1, 1.4, 0.2))


def test_a_bbox_without_a_page_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _citation(bbox=(0.1, 0.1, 0.2, 0.2))


def test_a_lab_document_with_zero_results_is_valid() -> None:
    """An empty extraction is an honest outcome, not a crash."""
    doc = LabDocument(
        document_id=101,
        extraction_metadata=ExtractionMetadata(
            model_id="stub", prompt_version="v0", extracted_at=datetime.now(),
            page_count=1, verified_fraction=0.0, unreadable_count=0, unverified_count=0,
        ),
    )
    assert doc.results == []
    assert doc.doc_type == "lab_pdf"


# --------------------------------------------------------------------------- #
# Behaviour: owed, and failing until it exists
# --------------------------------------------------------------------------- #


async def test_a_clean_lab_pdf_yields_the_printed_values() -> None:
    """F03: every CR2 field is extracted, and each value cites the page it came from."""
    from app.lab_extractor import extract_lab_document  # noqa: PLC0415

    doc = await extract_lab_document(
        document_id=101, pdf_bytes=CLEAN.read_bytes(), media_type="application/pdf"
    )
    a1c = next(r for r in doc.results if "A1c" in r.test_name)
    assert a1c.value == Decimal("8.9")
    assert a1c.unit == "%"
    assert a1c.reference_range == "4.0-5.6"
    assert a1c.collection_date == date(2026, 9, 12) or doc.collection_date == date(2026, 9, 12)
    assert a1c.citation.quote_or_value == "8.9"


async def test_no_printed_flag_is_never_reported_as_a_printed_flag() -> None:
    """The fixture prints no flags. 8.9 is above 4.0-5.6, but that is OUR comparison, not the lab's."""
    from app.lab_extractor import extract_lab_document  # noqa: PLC0415

    doc = await extract_lab_document(
        document_id=101, pdf_bytes=CLEAN.read_bytes(), media_type="application/pdf"
    )
    a1c = next(r for r in doc.results if "A1c" in r.test_name)
    assert a1c.abnormal_flag_source is not AbnormalFlagSource.EXTRACTED


async def test_an_obscured_value_is_marked_unreadable_not_guessed() -> None:
    """The degraded fixture prints '8.#'. Returning 8.9 by inference is the defect."""
    from app.lab_extractor import extract_lab_document  # noqa: PLC0415

    doc = await extract_lab_document(
        document_id=102, pdf_bytes=DEGRADED.read_bytes(), media_type="application/pdf"
    )
    a1c = next(r for r in doc.results if "A1c" in r.test_name)
    assert a1c.verification_status is VerificationStatus.UNREADABLE
    assert a1c.value is None


async def test_extraction_never_logs_the_document_bytes() -> None:
    """CR7: document images are sensitive. The base64 payload must not reach any sink."""
    import logging  # noqa: PLC0415

    from app.lab_extractor import extract_lab_document  # noqa: PLC0415

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(str(record.getMessage()))

    handler = _Capture()
    logging.getLogger().addHandler(handler)
    try:
        await extract_lab_document(
            document_id=101, pdf_bytes=CLEAN.read_bytes(), media_type="application/pdf"
        )
    finally:
        logging.getLogger().removeHandler(handler)

    payload = base64.b64encode(CLEAN.read_bytes()).decode()
    assert not any(payload[:64] in r for r in records)
