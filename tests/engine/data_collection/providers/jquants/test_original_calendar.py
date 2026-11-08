from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    RetainedRequest,
    complete_request,
)
from baibai_engine.data_collection.providers.jquants.native_index import (
    calendar_sessions,
    index_market_originals,
)


def receipt(div, *, at, day="2026-11-04"):
    query = {"from": "20261101", "to": "20261130"}
    page = JQuantsPage(
        "market_calendar", query, at, json.dumps({"data": [{"Date": day, "HolDiv": div}]}).encode()
    )
    return RetainedRequest(str(div), complete_request([page]), None)


def test_original_calendar_known_and_unknown():
    now = datetime(2026, 11, 4, tzinfo=UTC)
    day = date(2026, 11, 4)
    partial = RetainedRequest(
        "partial",
        None,
        "incomplete",
        {"from": "20261101", "to": "20261130"},
        now + timedelta(seconds=1),
    )
    cases = [
        ([receipt("1", at=now)], day, True),
        ([receipt("0", at=now)], day, False),
        ([receipt("1", at=now), receipt("0", at=now)], day, None),
        ([receipt("1", at=now)], date(2026, 11, 5), None),
        ([receipt("0", at=now), partial], day, None),
    ]
    for requests, selected_day, expected in cases:
        sessions, unknown = calendar_sessions(
            index_market_originals(requests, source="market_calendar"),
            start=selected_day,
            end=selected_day,
        )
        assert sessions == {selected_day: expected}
        assert unknown == ()
