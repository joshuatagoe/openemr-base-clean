"""The 12-month ageing rule for unreviewed document values - one source of truth.

A document whose values are still waiting for review more than
``UNREVIEWED_MAX_AGE_MONTHS`` after its date is not new: the document briefing
turns it into one Needs attention line (``app.document_briefing``), and a
follow-up answer citing one of its values must say it comes from an older
document that was never reviewed (``app.tools`` marks it, ``app.verifier``
enforces it).

A document's date is its latest collection date, else the date it was
received, else it is undated - and what cannot be dated is treated as recent,
never hidden.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from datetime import date
from typing import Literal

#: Months a document's values may stay unreviewed before it counts as an older document.
UNREVIEWED_MAX_AGE_MONTHS = 12

DateKind = Literal["collected", "received"]


def latest_date(collected: Iterable[date | None], received_at: date | None) -> tuple[date, DateKind] | None:
    """The latest collection date, else ``received_at`` (kind ``received``), else None (undated)."""
    dates = [d for d in collected if d is not None]
    if dates:
        return max(dates), "collected"
    if received_at is not None:
        return received_at, "received"
    return None


def is_aged(when: date, *, as_of: date) -> bool:
    """More than UNREVIEWED_MAX_AGE_MONTHS before ``as_of``; exactly that many months is not aged."""
    months = as_of.year * 12 + (as_of.month - 1) - UNREVIEWED_MAX_AGE_MONTHS
    year, month = divmod(months, 12)
    month += 1
    day = min(as_of.day, calendar.monthrange(year, month)[1])  # 29 Feb -> 28 Feb
    return when < date(year, month, day)


__all__ = ["UNREVIEWED_MAX_AGE_MONTHS", "DateKind", "is_aged", "latest_date"]
