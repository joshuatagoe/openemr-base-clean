"""TextractOcr: the AWS adapter behind the OcrSource port (ADR-007 s2, s8, s9, s11a).

Offline tests drive it with a stand-in client, so the request shape, response
parsing, bounded retry, timeout and failure codes are exercised without AWS.
The live test (opt-in) sends three synthetic pages to Textract in us-east-2 and
checks the boxes land on the ink.
"""

from __future__ import annotations

import io
import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.page_text import (
    DEFAULT_TEXTRACT_REGION,
    OCR_FAILED,
    OCR_TIMEOUT,
    OCR_UNAVAILABLE,
    OcrError,
    TextractOcr,
    orient_image,
    render_page_png,
)
from tests.test_page_text import ink

pytestmark = pytest.mark.anyio

DOCS = Path(__file__).resolve().parent.parent / "fixtures" / "documents" / "verification"


class _ClientError(Exception):
    """Shaped like botocore's ClientError: a fixed code under response['Error']['Code']."""

    def __init__(self, code: str) -> None:
        super().__init__(f"{code}: message that must never be logged SECRETMESSAGE")
        self.response = {"Error": {"Code": code, "Message": "SECRETMESSAGE"}}


def _block(text: str, left: float, top: float, width: float, height: float, kind: str = "WORD") -> dict[str, Any]:
    return {
        "BlockType": kind,
        "Text": text,
        "Confidence": 99.1,
        "TextType": "PRINTED",
        "Geometry": {"BoundingBox": {"Left": left, "Top": top, "Width": width, "Height": height}},
    }


RESPONSE = {
    "Blocks": [
        {"BlockType": "PAGE", "Geometry": {"BoundingBox": {"Left": 0, "Top": 0, "Width": 1, "Height": 1}}},
        _block("Hemoglobin A1c 8.9", 0.1, 0.2, 0.3, 0.02, kind="LINE"),
        _block("Hemoglobin", 0.1, 0.2, 0.12, 0.02),
        _block("8.9", 0.3, 0.2, 0.03, 0.02),
        _block("edge", -0.01, 0.5, 0.05, 0.02),  # partly off the image: dropped, never clamped
    ]
}


class _Client:
    def __init__(self, *script: Any, delay: float = 0.0) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.delay = delay

    def detect_document_text(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.page_text._backoff_sleep", _instant)


def test_the_default_region_is_us_east_2() -> None:
    """ADR-007 s11a: the organisation's SCP denies Textract in us-east-1 and us-west-2."""
    assert DEFAULT_TEXTRACT_REGION == "us-east-2"
    assert TextractOcr().region == "us-east-2"


async def test_word_blocks_become_words_in_the_image_frame() -> None:
    client = _Client(RESPONSE)
    words = await TextractOcr(client=client).read_page(b"png-bytes", page=3)
    assert [(w.text, w.page, w.source) for w in words] == [("Hemoglobin", 3, "ocr"), ("8.9", 3, "ocr")]
    assert words[1].bbox == pytest.approx((0.3, 0.2, 0.33, 0.22))
    assert client.calls == [{"Document": {"Bytes": b"png-bytes"}}]


async def test_a_throttle_is_retried_within_the_budget() -> None:
    client = _Client(_ClientError("ThrottlingException"), RESPONSE)
    words = await TextractOcr(client=client, max_attempts=2).read_page(b"x", page=1)
    assert len(client.calls) == 2
    assert any(w.text == "8.9" for w in words)


async def test_persistent_failure_stops_at_the_attempt_limit() -> None:
    client = _Client(_ClientError("InternalServerError"))
    with pytest.raises(OcrError) as caught:
        await TextractOcr(client=client, max_attempts=3).read_page(b"x", page=1)
    assert caught.value.reason == OCR_UNAVAILABLE
    assert len(client.calls) == 3


@pytest.mark.parametrize("code", ["AccessDeniedException", "UnsupportedDocumentException", "InvalidParameterException"])
async def test_a_permanent_error_is_not_retried(code: str) -> None:
    client = _Client(_ClientError(code))
    with pytest.raises(OcrError):
        await TextractOcr(client=client, max_attempts=3).read_page(b"x", page=1)
    assert len(client.calls) == 1


async def test_a_slow_call_times_out_with_a_fixed_code() -> None:
    client = _Client(RESPONSE, delay=1.0)
    started = time.perf_counter()
    with pytest.raises(OcrError) as caught:
        await TextractOcr(client=client, timeout_seconds=0.2).read_page(b"x", page=1)
    assert caught.value.reason == OCR_TIMEOUT
    assert time.perf_counter() - started < 0.9


async def test_a_malformed_response_is_a_failure_not_an_empty_page() -> None:
    with pytest.raises(OcrError) as caught:
        await TextractOcr(client=_Client({"Blocks": "nonsense"})).read_page(b"x", page=1)
    assert caught.value.reason == OCR_FAILED


async def test_errors_log_codes_never_messages_or_words(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client = _Client(_ClientError("ThrottlingException"), RESPONSE)
    await TextractOcr(client=client, max_attempts=2).read_page(b"x", page=1)
    assert "SECRETMESSAGE" not in caplog.text
    assert "Hemoglobin" not in caplog.text
    assert any(getattr(r, "reason_code", None) == "ThrottlingException" for r in caplog.records)


def test_the_client_is_built_lazily_with_sdk_retries_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3  # noqa: PLC0415 - the test inspects the adapter's call

    captured: dict[str, Any] = {}

    def _fake_client(service: str, **kwargs: Any) -> str:
        captured.update(kwargs, service=service)
        return "client"

    monkeypatch.setattr(boto3, "client", _fake_client)
    ocr = TextractOcr(region="us-east-2", timeout_seconds=7)
    assert captured == {}, "nothing is built until a call needs it"
    assert ocr.client() == "client"
    assert captured["service"] == "textract"
    assert captured["region_name"] == "us-east-2"
    config = captured["config"]
    assert config.retries == {"max_attempts": 0}
    assert config.read_timeout == 7 and config.connect_timeout == 7


# --------------------------------------------------------------------------- #
# Configuration (COPILOT_OCR, COPILOT_TEXTRACT_REGION, ..._TIMEOUT_SECONDS, COPILOT_OCR_RENDER_DPI)
# --------------------------------------------------------------------------- #


def test_settings_default_to_fake_ocr_in_us_east_2() -> None:
    from app.settings import ServiceSettings  # noqa: PLC0415

    settings = ServiceSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.ocr == "fake"
    assert settings.textract_region == DEFAULT_TEXTRACT_REGION == "us-east-2"
    assert settings.textract_timeout_seconds == 10.0
    assert settings.ocr_render_dpi == 200


def test_the_default_source_follows_copilot_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.page_text import FakeOcr, default_ocr_source  # noqa: PLC0415

    monkeypatch.setenv("COPILOT_OCR", "fake")
    assert isinstance(default_ocr_source(), FakeOcr)

    monkeypatch.setenv("COPILOT_OCR", "textract")
    monkeypatch.setenv("COPILOT_TEXTRACT_REGION", "us-east-2")
    monkeypatch.setenv("COPILOT_TEXTRACT_TIMEOUT_SECONDS", "4")
    source = default_ocr_source()
    assert isinstance(source, TextractOcr)
    assert source.region == "us-east-2" and source.timeout_seconds == 4
    assert default_ocr_source() is source, "one adapter (and one boto3 client) per configuration"


def test_the_render_dpi_follows_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.page_text import configured_render_dpi  # noqa: PLC0415

    monkeypatch.setenv("COPILOT_OCR_RENDER_DPI", "150")
    assert configured_render_dpi() == 150


# --------------------------------------------------------------------------- #
# Live: opt-in, synthetic pages only, ~$0.005
# --------------------------------------------------------------------------- #


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_TEXTRACT_LIVE_TEST") != "1",
    reason="set RUN_TEXTRACT_LIVE_TEST=1 (and AWS credentials) to call Textract with synthetic pages",
)
async def test_live_textract_reads_synthetic_pages_and_boxes_land_on_ink() -> None:
    from app.lab_extractor import LabDraft, LabResultDraft, extract_lab_document  # noqa: PLC0415
    from app.documents import VerificationStatus  # noqa: PLC0415
    from app.settings import ServiceSettings  # noqa: PLC0415
    from tests.fakes import FakeProvider  # noqa: PLC0415

    settings = ServiceSettings()
    ocr = TextractOcr(region=settings.textract_region, timeout_seconds=settings.textract_timeout_seconds)

    cases = [
        ("v_image_only.pdf", "application/pdf", "8.9"),
        ("v_form_handwritten.pdf", "application/pdf", "7.4"),
        ("v_photo_exif.jpg", "image/jpeg", "8.9"),
    ]
    for name, media_type, value in cases:
        data = (DOCS / name).read_bytes()
        row = LabResultDraft(test_name="Hemoglobin A1c", value_text=value, unit="%",
                             reference_range="4.0-5.6", quote=value, page=1)
        draft = LabDraft(collection_date="2026-09-12", page_count=1, results=[row])
        started = time.perf_counter()
        doc = await extract_lab_document(document_id=1, pdf_bytes=data, media_type=media_type,
                                         provider=FakeProvider(draft), ocr=ocr)
        elapsed = time.perf_counter() - started
        (result,) = doc.results
        assert result.verification_status in (VerificationStatus.VERIFIED_EXACT, VerificationStatus.VERIFIED_FUZZY), name
        page_image = (
            Image.open(io.BytesIO(render_page_png(data, 1))) if media_type == "application/pdf"
            else Image.open(io.BytesIO(orient_image(data)))
        )
        assert ink(page_image, result.citation.bbox) > 0.1, name
        print(f"{name}: {result.verification_status.value} in {elapsed:.2f}s")  # noqa: T201 - live evidence
