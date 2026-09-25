"""ADR-007 addition 3 (one media allow-list) and C2's ``printed_identity`` field.

The allow-list lives once, in ``app/documents.py``. GIF and WEBP are gone because
Textract cannot read them: a format the model accepts but the verifier cannot
check would produce values that can never be verified.
"""

from __future__ import annotations

import re
import typing
from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.documents import (
    SUPPORTED_MEDIA_TYPES,
    ExtractionMetadata,
    LabDocument,
    MediaType,
    PrintedIdentity,
)

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parents[2]
PHP_READER = REPO / "interface/modules/custom_modules/oe-module-copilot/src/Data/SqlDocumentReader.php"


def test_the_allow_list_is_exactly_pdf_png_and_jpeg() -> None:
    assert SUPPORTED_MEDIA_TYPES == frozenset({"application/pdf", "image/png", "image/jpeg"})


def test_the_literal_type_and_the_set_are_the_same_list() -> None:
    assert frozenset(typing.get_args(MediaType)) == SUPPORTED_MEDIA_TYPES


def test_the_php_reader_normalises_to_the_same_list() -> None:
    """The module decides what it sends; the agent decides what it accepts. They must agree."""
    source = PHP_READER.read_text(encoding="utf-8")
    targets = set(re.findall(r"'[a-z/.+-]+'\s*=>\s*'([a-z/.+-]+)'", source))
    assert targets == set(SUPPORTED_MEDIA_TYPES)


@pytest.mark.parametrize("media_type", ["image/gif", "image/webp", "image/tiff", "text/plain"])
async def test_the_extractor_refuses_anything_else(media_type: str) -> None:
    from app.lab_extractor import extract_lab_document  # noqa: PLC0415

    with pytest.raises(ValueError, match="unsupported"):
        await extract_lab_document(document_id=1, pdf_bytes=b"GIF89a", media_type=media_type)


def test_the_extractor_uses_the_shared_list() -> None:
    import app.lab_extractor as lab  # noqa: PLC0415

    assert lab.SUPPORTED_MEDIA_TYPES is SUPPORTED_MEDIA_TYPES


def _metadata() -> ExtractionMetadata:
    return ExtractionMetadata(
        model_id="stub", prompt_version="v", extracted_at=datetime.now(),
        page_count=1, verified_fraction=0.0, unreadable_count=0, unverified_count=0,
    )


def test_printed_identity_is_optional_and_defaults_to_none() -> None:
    assert LabDocument(document_id=1, extraction_metadata=_metadata()).printed_identity is None


def test_printed_identity_carries_a_name_and_a_date() -> None:
    doc = LabDocument(
        document_id=1,
        extraction_metadata=_metadata(),
        printed_identity=PrintedIdentity(name="Whitfield, Evelyn R.", dob=date(1981, 3, 14)),
    )
    assert doc.printed_identity is not None
    assert doc.printed_identity.dob == date(1981, 3, 14)


def test_printed_identity_fields_may_each_be_absent() -> None:
    assert PrintedIdentity().name is None
    assert PrintedIdentity(name=None, dob=None).dob is None


def test_printed_identity_rejects_invented_fields() -> None:
    with pytest.raises(ValidationError):
        PrintedIdentity(name="x", mrn="7")  # type: ignore[call-arg]
