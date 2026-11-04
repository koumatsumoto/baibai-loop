"""Select latest complete native full-day requests without reading projections."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from datetime import date

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsRequest,
    JQuantsSource,
    RetainedRequest,
)


def select_full_days(
    requests: Iterable[RetainedRequest], *, source: JQuantsSource, days: set[date]
) -> tuple[dict[date, tuple[str, JQuantsRequest]], dict[date, int]]:
    """Return complete receipts and count incomplete evidence per requested day."""
    selected: dict[date, tuple[str, JQuantsRequest]] = {}
    ambiguous: set[date] = set()
    incomplete = dict.fromkeys(days, 0)
    for item in requests:
        request = item.request
        if request is None:
            affected = days
            if item.first_query is not None and set(item.first_query) == {"date"}:
                with suppress(ValueError):
                    affected = {date.fromisoformat(item.first_query["date"])} & days
            for day in affected:
                incomplete[day] += 1
            continue
        if request.source != source or set(request.params) != {"date"}:
            continue
        try:
            day = date.fromisoformat(request.params["date"])
        except ValueError:
            for affected_day in days:
                incomplete[affected_day] += 1
            continue
        if day not in days:
            continue
        prior = selected.get(day)
        if prior is None or request.received_at > prior[1].received_at:
            selected[day] = item.request_id, request
            ambiguous.discard(day)
        elif request.received_at == prior[1].received_at and request.pages != prior[1].pages:
            ambiguous.add(day)
    if ambiguous:
        raise ValueError("receipts have an ambiguous latest time")
    return selected, incomplete
