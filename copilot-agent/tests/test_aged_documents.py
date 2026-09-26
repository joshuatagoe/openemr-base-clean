"""Old unreviewed values age out of the briefing (Milestone 4).

A stored document whose values are still waiting for review, but which is more
than UNREVIEWED_MAX_AGE_MONTHS older than the briefing date, is not briefed as
new facts: it gets one Needs attention line saying an older document has values
nobody has reviewed, citing it. Its date is its latest collection date, else
the date it was received (``received_at``), else it is treated as recent -
what cannot be dated is never hidden. Intake forms are dated by ``received_at``.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

import pytest

from app.document_briefing import (
    UNREVIEWED_MAX_AGE_MONTHS,
    DocumentBriefingRequest,
    StoredDocument,
    build_reranker,
    document_date,
    is_aged,
)
from app.workflow import run_supervised_briefing
from tests.test_intake_briefing import _intake, _stored_intake
from tests.test_stored_documents import _AnswerOnly, _extraction, _payload, _stored

AS_OF = date(2026, 9, 26)
AGED = "An older document"


def _dated(extraction: dict[str, Any], collected: str | None, per_result: list[str | None] | None = None) -> dict[str, Any]:
    out = copy.deepcopy(extraction)
    out["collection_date"] = collected
    for i, r in enumerate(out["results"]):
        r["collection_date"] = per_result[i] if per_result is not None and i < len(per_result) else None
    return out


async def _brief(documents: list[dict[str, Any]], as_of: date = AS_OF) -> Any:
    request = DocumentBriefingRequest.model_validate(_payload(documents))
    return await run_supervised_briefing(
        request, provider=_AnswerOnly(), reranker=build_reranker("fake", region="us-east-1"), as_of=as_of
    )


def _lines(response: Any) -> list[Any]:
    return list(response.briefing.what_changed) + list(response.briefing.needs_attention)


def _cited(lines: list[Any]) -> set[str]:
    return {ln.document_citation.source_id for ln in lines if ln.document_citation is not None}


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #


def test_the_age_limit_is_twelve_months() -> None:
    assert UNREVIEWED_MAX_AGE_MONTHS == 12


@pytest.mark.parametrize(
    ("when", "aged"),
    [
        (date(2025, 9, 26), False),  # exactly 12 months: still briefed
        (date(2025, 9, 25), True),  # one day more
        (date(2026, 9, 26), False),
        (date(2027, 1, 1), False),  # a future date is not old
    ],
)
def test_older_than_twelve_months_is_the_boundary(when: date, aged: bool) -> None:
    assert is_aged(when, as_of=AS_OF) is aged


def test_a_leap_day_briefing_date_has_a_valid_cutoff() -> None:
    assert is_aged(date(2023, 2, 28), as_of=date(2024, 2, 29)) is False
    assert is_aged(date(2023, 2, 27), as_of=date(2024, 2, 29)) is True


@pytest.mark.anyio
async def test_a_document_is_dated_by_its_latest_collection_date_then_received_at() -> None:
    base = await _extraction(201)
    newest = StoredDocument.model_validate(
        {**_stored(201, _dated(base, "2024-01-01", ["2024-01-01", "2026-05-01"])), "received_at": "2023-01-01"}
    )
    assert document_date(newest) == (date(2026, 5, 1), "collected")
    received = StoredDocument.model_validate({**_stored(201, _dated(base, None)), "received_at": "2025-02-03"})
    assert document_date(received) == (date(2025, 2, 3), "received")
    undated = StoredDocument.model_validate(_stored(201, _dated(base, None)))
    assert document_date(undated) is None


# --------------------------------------------------------------------------- #
# The briefing
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_an_old_lab_is_one_needs_attention_line_not_new_facts() -> None:
    old = _dated(await _extraction(301), "2025-08-01")
    recent = _dated(await _extraction(302), "2026-09-12")
    response = await _brief([_stored(301, old), _stored(302, recent)])
    briefing = response.briefing
    assert "301" not in _cited(list(briefing.what_changed))
    aged = [ln for ln in briefing.needs_attention if ln.document_citation and ln.document_citation.source_id == "301"]
    n = len(old["results"])
    assert [ln.text for ln in aged] == [f"An older document (collected 2025-08-01) has {n} value(s) nobody has reviewed."]
    assert aged[0].not_yet_in_chart
    # The recent document is briefed as before; considerations never draw on the old one.
    assert "302" in _cited(list(briefing.what_changed))
    for c in briefing.what_to_consider:
        assert all(f.document_citation is None or f.document_citation.source_id == "302" for f in c.facts)
    assert response.document_ids == (301, 302)


@pytest.mark.anyio
async def test_exactly_twelve_months_old_is_still_briefed() -> None:
    response = await _brief([_stored(301, _dated(await _extraction(301), "2025-09-26"))])
    assert not any(AGED in ln.text for ln in _lines(response))
    assert "301" in _cited(list(response.briefing.what_changed))


@pytest.mark.anyio
async def test_an_undated_document_is_never_hidden() -> None:
    response = await _brief([_stored(301, _dated(await _extraction(301), None))], as_of=date(2030, 1, 1))
    assert not any(AGED in ln.text for ln in _lines(response))
    assert "301" in _cited(list(response.briefing.what_changed))


@pytest.mark.anyio
async def test_received_at_dates_a_lab_without_a_collection_date() -> None:
    stored = {**_stored(301, _dated(await _extraction(301), None)), "received_at": "2025-01-10"}
    response = await _brief([stored])
    texts = [ln.text for ln in response.briefing.needs_attention]
    assert any(t.startswith("An older document (received 2025-01-10) has ") for t in texts)
    assert not response.briefing.what_changed


@pytest.mark.anyio
async def test_a_recent_collection_date_wins_over_an_old_received_at() -> None:
    stored = {**_stored(301, _dated(await _extraction(301), "2026-09-01")), "received_at": "2024-01-10"}
    response = await _brief([stored])
    assert not any(AGED in ln.text for ln in _lines(response))


@pytest.mark.anyio
async def test_only_old_documents_still_render_a_briefing() -> None:
    response = await _brief([_stored(301, _dated(await _extraction(301), "2024-03-01"))])
    assert response.briefing is not None
    assert not response.briefing.what_changed and not response.briefing.what_to_consider
    assert [ln.line_id for ln in response.briefing.needs_attention] == ["aged-301"]


def test_received_at_survives_the_json_round_trip_with_numeric_values() -> None:
    import asyncio
    from decimal import Decimal

    stored = {**_stored(101, asyncio.run(_extraction(101))), "received_at": "2025-01-10"}
    doc = StoredDocument.model_validate(stored)
    again = StoredDocument.model_validate(doc.model_dump(mode="json"))
    assert again.received_at == date(2025, 1, 10)
    assert isinstance(again.extraction.results[0].value, Decimal)


# --------------------------------------------------------------------------- #
# Intake forms: dated by received_at
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_an_old_intake_form_is_one_line_and_a_recent_one_is_briefed() -> None:
    form = await _intake(401)
    old = {**_stored_intake(401, form), "received_at": "2025-06-01"}
    response = await _brief([old])
    lines = _lines(response)
    assert not any(ln.line_id.startswith("intake-") for ln in lines)
    aged = [ln for ln in lines if ln.line_id == "aged-401"]
    assert len(aged) == 1 and aged[0].text.startswith("An older document (received 2025-06-01) has ")
    assert aged[0].document_citation.source_id == "401"

    recent = {**_stored_intake(401, form), "received_at": "2026-09-01"}
    response = await _brief([recent])
    assert any(ln.line_id.startswith("intake-401-") for ln in _lines(response))
    assert not any(AGED in ln.text for ln in _lines(response))
