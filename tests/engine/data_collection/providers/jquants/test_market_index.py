from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    RetainedRequest,
    complete_request,
)
from baibai_engine.data_collection.providers.jquants.market_index import index_market_originals


def receipt(identity, source, params, rows):
    page = JQuantsPage(
        source, params, datetime(2026, 11, 4, tzinfo=UTC), json.dumps({"data": rows}).encode()
    )
    return RetainedRequest(identity, complete_request([page]), None)


def test_full_empty_and_filtered_requests_are_distinct():
    full = receipt("full", "daily_bars", {"date": "20261102"}, [])
    filtered = receipt("filtered", "daily_bars", {"date": "20261102", "code": "13010"}, [{}])
    calendar = receipt("calendar", "market_calendar", {"from": "20261101", "to": "20261130"}, [{}])
    partial = RetainedRequest("partial", None, "incomplete")
    index = index_market_originals([full, filtered, partial], source="daily_bars")
    calendars = index_market_originals([calendar], source="market_calendar")
    assert [(x.request_id, x.start, x.end, x.records) for x in index.receipts] == [
        ("full", date(2026, 11, 2), date(2026, 11, 2), 0),
    ]
    assert [item.request_id for item in index.incomplete] == ["partial"]
    assert calendars.receipts[0].end == date(2026, 11, 30)


def test_same_page_identity_cannot_have_different_original_bytes():
    first = RetainedRequest("r", None, "partial", page_fingerprints=((0, "a"),))
    second = RetainedRequest("r", None, "partial", page_fingerprints=((0, "b"),))
    with pytest.raises(ValueError, match="conflicting retained page"):
        index_market_originals([first, second], source="daily_bars")
