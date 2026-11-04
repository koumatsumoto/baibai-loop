from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from baibai_engine.data_collection.providers.jquants.calendar import business_day_from_originals
from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    RetainedRequest,
    complete_request,
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
    assert business_day_from_originals([receipt("1", at=now)], day) is True
    assert business_day_from_originals([receipt("0", at=now)], day) is False
    assert business_day_from_originals([receipt("1", at=now), receipt("0", at=now)], day) is None
    assert business_day_from_originals([receipt("1", at=now)], date(2026, 11, 5)) is None
    partial = RetainedRequest(
        "partial",
        None,
        "incomplete",
        {"from": "20261101", "to": "20261130"},
        now + timedelta(seconds=1),
    )
    assert business_day_from_originals([receipt("0", at=now), partial], day) is None
