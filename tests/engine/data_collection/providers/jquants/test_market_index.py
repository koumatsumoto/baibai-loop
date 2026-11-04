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


def test_price_plan_retries_missing_or_newer_partial_and_preserves_empty():
    from baibai_engine.data_collection.providers.jquants.market_index import plan_daily_prices

    first = receipt("first", "daily_bars", {"date": "20261101"}, [{"Code": "13010"}])
    empty = receipt("empty", "daily_bars", {"date": "20261102"}, [])
    partial = RetainedRequest(
        "partial", None, "incomplete", {"date": "20261102"}, datetime(2026, 11, 5, tzinfo=UTC)
    )
    index = index_market_originals([first, empty, partial], source="daily_bars")
    plan = plan_daily_prices(index, start=date(2026, 11, 1), end=date(2026, 11, 3))
    assert plan.days == (date(2026, 11, 2), date(2026, 11, 3))
    assert plan.unknown_incomplete_requests == ()


def test_price_plan_refreshes_trailing_empty_days_and_reports_unknown():
    from baibai_engine.data_collection.providers.jquants.market_index import plan_daily_prices

    first = receipt("first", "daily_bars", {"date": "20261101"}, [{"Code": "13010"}])
    second = receipt("second", "daily_bars", {"date": "20261102"}, [])
    unknown = RetainedRequest("unknown", None, "incomplete")
    plan = plan_daily_prices(
        index_market_originals([first, second, unknown], source="daily_bars"),
        start=date(2026, 11, 1),
        end=date(2026, 11, 2),
    )
    assert plan.days == (date(2026, 11, 1), date(2026, 11, 2))
    assert plan.unknown_incomplete_requests == ("unknown",)


def test_calendar_plan_checks_each_day_and_partial_overlap():
    from baibai_engine.data_collection.providers.jquants.market_index import plan_calendar

    calendar = receipt(
        "calendar",
        "market_calendar",
        {"from": "20261101", "to": "20261103"},
        [{"Date": "2026-11-01", "HolDiv": "0"}, {"Date": "2026-11-02", "HolDiv": "1"}],
    )
    partial = RetainedRequest(
        "partial",
        None,
        "incomplete",
        {"from": "20261102", "to": "20261102"},
        datetime(2026, 11, 5, tzinfo=UTC),
    )
    index = index_market_originals([calendar, partial], source="market_calendar")
    plan = plan_calendar(index, start=date(2026, 11, 1), end=date(2026, 11, 3))
    assert plan.days == (date(2026, 11, 2), date(2026, 11, 3))


def test_calendar_invalid_record_is_not_accepted_as_clean():
    from baibai_engine.data_collection.providers.jquants.market_index import plan_calendar

    source = receipt(
        "calendar",
        "market_calendar",
        {"from": "20261101", "to": "20261101"},
        [{"Date": "2026-11-01", "HolDiv": None}],
    )
    plan = plan_calendar(
        index_market_originals([source], source="market_calendar"),
        start=date(2026, 11, 1),
        end=date(2026, 11, 1),
    )
    assert plan.days == (date(2026, 11, 1),)


def test_calendar_one_bad_day_does_not_discard_other_days():
    from baibai_engine.data_collection.providers.jquants.market_index import plan_calendar

    source = receipt(
        "calendar",
        "market_calendar",
        {"from": "20261101", "to": "20261102"},
        [{"Date": "2026-11-01", "HolDiv": "0"}, {"Date": "2026-11-02", "HolDiv": None}],
    )
    plan = plan_calendar(
        index_market_originals([source], source="market_calendar"),
        start=date(2026, 11, 1),
        end=date(2026, 11, 2),
    )
    assert plan.days == (date(2026, 11, 2),)
