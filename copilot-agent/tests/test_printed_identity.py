"""C2 / ADR-012: the name and date of birth printed on the report reach the module, and nothing else.

The agent reads them; the OpenEMR module compares them with the chart and holds
a mismatch back. They are PHI, so they must never reach a log or a span.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pytest

from app.documents import PrintedIdentity
from app.lab_extractor import LabDraft, LabResultDraft, extract_lab_document
from app.providers.prompt import LAB_EXTRACTION_PROMPT_VERSION, LAB_EXTRACTION_SYSTEM_PROMPT
from app.recording import RECORDINGS_DIR, ReplayProvider
from tests.fakes import FakeProvider

pytestmark = pytest.mark.anyio

CLEAN = Path(__file__).resolve().parent.parent / "fixtures" / "documents" / "lab_hba1c_clean.pdf"


def _draft(**identity: str | None) -> LabDraft:
    row = LabResultDraft(test_name="Hemoglobin A1c", value_text="8.9", unit="%", quote="8.9", page=1)
    return LabDraft(page_count=1, results=[row], **identity)


async def _extract(draft: LabDraft):
    return await extract_lab_document(document_id=4, pdf_bytes=CLEAN.read_bytes(),
                                      media_type="application/pdf", provider=FakeProvider(draft))


def test_the_draft_asks_the_model_for_the_printed_name_and_dob() -> None:
    fields = LabDraft.model_fields
    assert "patient_name" in fields and "patient_dob" in fields
    assert fields["patient_name"].default is None and fields["patient_dob"].default is None


def test_the_prompt_asks_for_them_and_says_what_they_are_for() -> None:
    prompt = LAB_EXTRACTION_SYSTEM_PROMPT
    assert "patient_name" in prompt and "patient_dob" in prompt
    assert LAB_EXTRACTION_PROMPT_VERSION == "lab-v2"


def test_the_prompt_no_longer_asks_the_model_to_verify_or_locate() -> None:
    """Verification and boxes are the application's (ADR-007); the prompt must not claim otherwise."""
    prompt = LAB_EXTRACTION_SYSTEM_PROMPT
    assert "verified_exact" not in prompt
    assert "bbox" not in prompt


async def test_the_printed_identity_is_returned_on_the_document() -> None:
    doc = await _extract(_draft(patient_name="Whitfield, Evelyn R.", patient_dob="1981-03-14"))
    assert doc.printed_identity == PrintedIdentity(name="Whitfield, Evelyn R.", dob=date(1981, 3, 14))


async def test_an_unparseable_dob_keeps_the_name_and_drops_the_date() -> None:
    doc = await _extract(_draft(patient_name="Whitfield, Evelyn R.", patient_dob="14th March"))
    assert doc.printed_identity == PrintedIdentity(name="Whitfield, Evelyn R.", dob=None)


async def test_no_printed_identity_is_none_not_an_empty_object() -> None:
    doc = await _extract(_draft())
    assert doc.printed_identity is None


async def test_the_recorded_model_reads_the_identity_off_the_fixture() -> None:
    """The real model's recorded output for the clean fixture carries the printed name and DOB."""
    recorded = json.loads((RECORDINGS_DIR / "lab_clean_hba1c.json").read_text(encoding="utf-8"))
    doc = await extract_lab_document(document_id=101, pdf_bytes=CLEAN.read_bytes(), media_type="application/pdf",
                                     provider=ReplayProvider("lab_clean_hba1c", recorded["model"]))
    assert doc.printed_identity == PrintedIdentity(name="Whitfield, Evelyn R.", dob=date(1981, 3, 14))


async def test_the_identity_never_reaches_a_log(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    await _extract(_draft(patient_name="Zyxwvut, Canary Q.", patient_dob="1911-11-11"))
    assert "Zyxwvut" not in caplog.text
    assert "1911" not in caplog.text
    for record in caplog.records:
        assert "Zyxwvut" not in str(record.__dict__)
