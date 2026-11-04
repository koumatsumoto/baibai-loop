"""Resolve a cash-equity session directly from native calendar receipts."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest
from baibai_engine.data_collection.providers.jquants.dependencies import dependency_covers_day


def business_day_from_originals(requests: Iterable[RetainedRequest], day: date) -> bool | None:
    """Unknown or conflicting originals cannot justify skipping acquisition."""
    observations: list[tuple[datetime, bool | None]] = []
    for item in requests:
        request = item.request
        query = dict(request.params) if request else item.first_query
        if not query or not dependency_covers_day("market_calendar", query, day):
            if request is None and not query:
                return None
            continue
        if request is None:
            if item.last_received_at is None:
                return None
            observations.append((item.last_received_at, None))
            continue
        if request.source != "market_calendar":
            continue
        matching = []
        for row in request.records():
            try:
                record_day = date.fromisoformat(str(row.get("Date", ""))[:10])
            except ValueError:
                return None
            if record_day == day:
                matching.append(row)
        divisions = {str(row.get("HolDiv")) for row in matching}
        value = (
            next(iter(divisions)) in {"1", "2"}
            if len(divisions) == 1 and divisions <= {"0", "1", "2"}
            else None
        )
        observations.append((request.received_at, value))
    if not observations:
        return None
    latest = max(observed_at for observed_at, _ in observations)
    values = {value for observed_at, value in observations if observed_at == latest}
    return next(iter(values)) if len(values) == 1 else None
