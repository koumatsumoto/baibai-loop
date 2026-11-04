"""Resolve a cash-equity session directly from native calendar receipts."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest
from baibai_engine.data_collection.providers.jquants.native_index import (
    calendar_sessions,
    index_market_originals,
)


def business_day_from_originals(requests: Iterable[RetainedRequest], day: date) -> bool | None:
    """Unknown or conflicting originals cannot justify skipping acquisition."""
    index = index_market_originals(requests, source="market_calendar")
    sessions, unknown = calendar_sessions(index, start=day, end=day)
    return None if unknown else sessions[day]
