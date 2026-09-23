"""The ModelProvider port is narrow, and stays narrow.

Week 2 adds document types (lab_pdf, intake_form, and later a third). The
failure mode this guards against is adding one provider method per document
type: every implementation, including StubProvider and every test fake, would
then grow a body it does not use.

See W2_PLANNING/W2_ARCHITECTURE_DECISIONS.md, "Design principles" -
interface segregation and open/closed.
"""

from __future__ import annotations

import base64

import pytest

from app.providers.base import (
    DocumentPart,
    ModelExtractionOutput,
    ModelProvider,
    ProviderConfigurationError,
    StrictModel,
    TextPart,
)
from app.providers.stub_provider import StubProvider

pytestmark = pytest.mark.anyio

EXPECTED_MEMBERS = {"name", "parse_structured", "turn_step", "tool_results_message", "ping"}


def test_port_surface_does_not_grow() -> None:
    """A new document type must add a schema, not a member here."""
    actual = {m for m in ModelProvider.__protocol_attrs__ if not m.startswith("_")}
    assert actual == EXPECTED_MEMBERS, (
        "ModelProvider gained or lost a member. Adding a document type should add a "
        "schema and zero members; if you are adding one, the seam is wrong."
    )


def test_extract_commitments_is_not_part_of_the_port() -> None:
    """Week 1's entry point survives as a convenience method, not a contract."""
    assert "extract_commitments" not in ModelProvider.__protocol_attrs__


class _UnknownSchema(StrictModel):
    value: str


async def test_stub_refuses_a_schema_it_has_no_fixture_for() -> None:
    """Silence would be worse than refusal: a fabricated instance looks like a real extraction."""
    with pytest.raises(ProviderConfigurationError):
        await StubProvider().parse_structured(
            system="s", content=[TextPart(text="x")], schema=_UnknownSchema, max_tokens=16
        )


async def test_a_new_schema_is_added_by_registration_alone() -> None:
    """The open/closed consequence, exercised: no edit to StubProvider, no edit to the port."""
    StubProvider.register_fixture(_UnknownSchema, lambda _content: _UnknownSchema(value="fixture"))
    try:
        result = await StubProvider().parse_structured(
            system="s", content=[TextPart(text="x")], schema=_UnknownSchema, max_tokens=16
        )
        assert result.output.value == "fixture"
    finally:
        StubProvider._fixtures.pop(_UnknownSchema, None)


async def test_port_accepts_a_document_part() -> None:
    """The reason the port changed at all: a scanned PDF has no bare-string form."""
    pdf = DocumentPart(media_type="application/pdf", data_base64=base64.b64encode(b"%PDF-1.4").decode())
    StubProvider.register_fixture(ModelExtractionOutput, lambda _c: ModelExtractionOutput())
    try:
        result = await StubProvider().parse_structured(
            system="s", content=[pdf], schema=ModelExtractionOutput, max_tokens=16
        )
        assert result.output.commitments == []
    finally:
        StubProvider._fixtures.pop(ModelExtractionOutput, None)
