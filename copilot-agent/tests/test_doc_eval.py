"""The Week 2 document tier of the golden set.

Each case replays a REAL model response recorded once against a synthetic lab
PDF. These tests pin two things: every committed case passes on its recording,
and the tier fails loudly - rather than passing silently - when a recording no
longer describes the code under test.
"""

from __future__ import annotations

import pytest

import app.doc_eval as doc_eval
from app.doc_eval import load_doc_cases, score_doc_case

MODEL = "claude-opus-5"
CASES = load_doc_cases()


def test_there_are_document_cases_and_three_come_from_the_starter_set() -> None:
    assert len(CASES) >= 5
    assert sum(1 for c in CASES if c["case_id"].startswith("starter_")) >= 3


@pytest.mark.parametrize("case", CASES, ids=[c["case_id"] for c in CASES])
def test_every_document_case_passes_on_its_recording(case: dict) -> None:
    result = score_doc_case(case, model=MODEL)
    assert result.passed, result.failures


def test_a_different_model_makes_every_case_fail_rather_than_pass() -> None:
    """Evidence recorded against one model says nothing about another."""
    result = score_doc_case(CASES[0], model="claude-haiku-4-5")
    assert result.scores["schema_valid"] is False
    assert any("stale" in f for f in result.failures)


def test_a_changed_prompt_makes_the_case_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """The grader's scenario: change a prompt and the gate must block."""
    import app.lab_extractor as lab

    monkeypatch.setattr(lab, "LAB_EXTRACTION_SYSTEM_PROMPT", lab.LAB_EXTRACTION_SYSTEM_PROMPT + " Round every value.")
    result = score_doc_case(CASES[0], model=MODEL)
    assert result.scores["schema_valid"] is False
    assert any("prompt changed" in f for f in result.failures)


def test_an_invented_value_on_a_degraded_scan_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scoring must reject a guessed value even when the replay itself is valid."""
    degraded = next(c for c in CASES if c["case_id"] == "lab_degraded_scan")
    forged = {**degraded, "expect": {**degraded["expect"], "allowed_values": ["8.9"]}}
    result = score_doc_case(forged, model=MODEL)
    assert result.scores["factually_consistent"] is False


def test_the_tier_reads_documents_and_recordings_from_the_committed_fixtures() -> None:
    for case in CASES:
        document = case.get("document") or case["pdf"]  # intake cases name an image or PDF
        assert (doc_eval.DOCUMENTS_DIR / document).exists(), document


# --------------------------------------------------------------------------- #
# Intake forms (ADR-010)
# --------------------------------------------------------------------------- #

INTAKE_CASES = [c for c in CASES if c.get("kind") == "intake"]


def _intake(case_id: str) -> dict:
    return next(c for c in INTAKE_CASES if c["case_id"] == case_id)


def test_intake_cases_cover_a_typed_form_a_blank_section_and_a_handwritten_photo() -> None:
    classes = {c["test_class"] for c in INTAKE_CASES}
    assert {"intake_clean", "intake_blank_section", "intake_handwritten"} <= classes
    photo = _intake("intake_handwritten_photo")
    assert photo["media_type"] == "image/jpeg" and photo["ocr"] == "recorded"
    assert (doc_eval.OCR_RECORDINGS_DIR / "intake_handwritten_photo.json").exists()


def test_a_changed_intake_prompt_makes_the_intake_cases_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.intake_extractor as intake

    monkeypatch.setattr(intake, "INTAKE_EXTRACTION_SYSTEM_PROMPT", intake.INTAKE_EXTRACTION_SYSTEM_PROMPT + " Guess.")
    result = score_doc_case(_intake("intake_typed_evelyn"), model=MODEL)
    assert result.scores["schema_valid"] is False
    assert any("prompt changed" in f for f in result.failures)


def test_an_invented_no_known_allergies_fails_safe_refusal() -> None:
    import asyncio

    from app.intake import IntakeField
    from app.documents import DocumentCitation, VerificationStatus
    from app.intake_extractor import extract_intake_document
    from app.page_text import FakeOcr
    from app.recording import ReplayProvider

    case = _intake("intake_typed_blank_allergies")
    document = (doc_eval.DOCUMENTS_DIR / case["document"]).read_bytes()
    form = asyncio.run(extract_intake_document(
        document_id=case["document_id"], document_bytes=document, media_type=case["media_type"],
        provider=ReplayProvider(case["case_id"], MODEL), ocr=FakeOcr(),
    ))
    assert doc_eval.score_intake_form(case, form, "", document).passed
    forged = form.model_copy(update={"allergies_none_stated": IntakeField(
        value="No known allergies", verification_status=VerificationStatus.UNVERIFIED,
        citation=DocumentCitation(source_id=str(case["document_id"]), page_or_section="p. 1",
                                  field_or_chunk_id="allergies_none_stated", quote_or_value="No known allergies"),
    )})
    result = doc_eval.score_intake_form(case, forged, "", document)
    assert result.scores["safe_refusal"] is False and result.scores["factually_consistent"] is False


def test_a_changed_photo_makes_its_ocr_recording_stale() -> None:
    from app.recording import StaleRecordingError

    with pytest.raises(StaleRecordingError):
        doc_eval.recorded_ocr(_intake("intake_handwritten_photo"), b"other bytes")
