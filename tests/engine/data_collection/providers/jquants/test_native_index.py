from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    RetainedRequest,
    complete_request,
)
from baibai_engine.data_collection.providers.jquants.native_index import index_market_originals


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
    from baibai_engine.data_collection.providers.jquants.native_index import plan_daily_prices

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
    from baibai_engine.data_collection.providers.jquants.native_index import plan_daily_prices

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
    from baibai_engine.data_collection.providers.jquants.native_index import plan_calendar

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
    from baibai_engine.data_collection.providers.jquants.native_index import plan_calendar

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
    from baibai_engine.data_collection.providers.jquants.native_index import plan_calendar

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


def test_required_days_distinguishes_nonempty_rule_and_partial():
    from baibai_engine.data_collection.providers.jquants.native_index import plan_required_days

    empty = receipt("empty", "margin_interest", {"date": "20261102"}, [])
    full = receipt("full", "margin_interest", {"date": "20261103"}, [{"Code": "13010"}])
    index = index_market_originals([empty, full], source="margin_interest")
    requested = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]
    assert plan_required_days(index, days=requested, require_nonempty=True).days == (
        date(2026, 11, 2),
        date(2026, 11, 4),
    )
    assert plan_required_days(index, days=requested, require_nonempty=False).days == (
        date(2026, 11, 4),
    )


def test_calendar_sessions_preserve_missing_days():
    from baibai_engine.data_collection.providers.jquants.native_index import calendar_sessions

    source = receipt(
        "calendar",
        "market_calendar",
        {"from": "20261101", "to": "20261103"},
        [{"Date": "2026-11-01", "HolDiv": "0"}, {"Date": "2026-11-02", "HolDiv": "1"}],
    )
    sessions, unknown = calendar_sessions(
        index_market_originals([source], source="market_calendar"),
        start=date(2026, 11, 1),
        end=date(2026, 11, 3),
    )
    assert sessions == {date(2026, 11, 1): False, date(2026, 11, 2): True, date(2026, 11, 3): None}
    assert unknown == ()


def test_daily_margin_waits_for_next_session_and_retains_calendar_gaps():
    from baibai_engine.data_collection.providers.jquants.native_index import plan_daily_margin

    calendar = receipt(
        "calendar",
        "market_calendar",
        {"from": "20261102", "to": "20261105"},
        [
            {"Date": "2026-11-02", "HolDiv": "1"},
            {"Date": "2026-11-03", "HolDiv": "0"},
            {"Date": "2026-11-04", "HolDiv": "1"},
        ],
    )
    margin = receipt("margin", "margin_interest", {"date": "20261102"}, [])
    result = plan_daily_margin(
        index_market_originals([margin], source="margin_interest"),
        index_market_originals([calendar], source="market_calendar"),
        first_balance_date=date(2026, 11, 2),
        asof=date(2026, 11, 5),
    )
    assert result.days == (date(2026, 11, 2),)
    assert result.unknown_calendar_days == (date(2026, 11, 5),)


def test_weekly_margin_does_not_refresh_postpublication_empty():
    from baibai_engine.data_collection.providers.jquants.native_index import plan_weekly_margin

    rows = [
        {"Date": f"2026-09-{day:02}", "HolDiv": "1" if day in {14, 15, 16, 17, 18, 21, 22} else "0"}
        for day in range(14, 23)
    ]
    calendar = receipt("calendar", "market_calendar", {"from": "20260914", "to": "20260922"}, rows)
    empty = receipt("empty", "margin_interest", {"date": "20260918"}, [])
    result = plan_weekly_margin(
        index_market_originals([empty], source="margin_interest"),
        index_market_originals([calendar], source="market_calendar"),
        start=date(2026, 9, 14),
        asof=date(2026, 9, 22),
        final_balance_date=date(2026, 9, 18),
        final_publication_date=date(2026, 9, 25),
    )
    assert result.days == ()
    assert result.unknown_calendar_days == ()


def test_weekly_margin_refreshes_empty_received_before_publication():
    from baibai_engine.data_collection.providers.jquants.native_index import plan_weekly_margin

    rows = [
        {"Date": f"2026-09-{day:02}", "HolDiv": "1" if day in {14, 15, 16, 17, 18, 21, 22} else "0"}
        for day in range(14, 23)
    ]
    calendar = receipt("calendar", "market_calendar", {"from": "20260914", "to": "20260922"}, rows)
    page = JQuantsPage(
        "margin_interest", {"date": "20260918"}, datetime(2026, 9, 21, tzinfo=UTC), b'{"data":[]}'
    )
    early = RetainedRequest("early", complete_request([page]), None)
    result = plan_weekly_margin(
        index_market_originals([early], source="margin_interest"),
        index_market_originals([calendar], source="market_calendar"),
        start=date(2026, 9, 14),
        asof=date(2026, 9, 22),
        final_balance_date=date(2026, 9, 18),
        final_publication_date=date(2026, 9, 25),
    )
    assert result.days == (date(2026, 9, 18),)
