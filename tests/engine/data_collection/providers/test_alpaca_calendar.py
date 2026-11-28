from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from baibai_engine.data_collection.providers.alpaca_calendar import (
    AlpacaCalendarResponse,
    read_paper_us_calendar,
)

_PAPER = "https://paper-api.alpaca.markets/v2/calendar"
_REGULAR = date(2026, 11, 25)
_HOLIDAY = date(2026, 11, 26)
_SHORT = date(2026, 11, 27)
_WEEKEND = date(2026, 11, 28)
_NY = ZoneInfo("America/New_York")
_AT = datetime(2026, 11, 27, 8, 43, tzinfo=_NY)


def _row(day, *, opened="09:30", closed="16:00"):
    return {
        "date": day.isoformat(),
        "open": opened,
        "close": closed,
        "session_open": "0400",
        "session_close": "2000",
        "settlement_date": "2026-11-30",
    }


def _fixture(url, body):
    return AlpacaCalendarResponse(url, 200, _AT, body, True)


def _read(body, *, start=_SHORT, end=None):
    return read_paper_us_calendar(
        start, start if end is None else end, get_authorized=lambda url: _fixture(url, body)
    )


def test_one_bounded_paper_trading_request_distinguishes_normal_day_shortened_day_and_closures():
    urls = []

    def fetch(url):
        urls.append(url)
        return _fixture(url, json.dumps([_row(_SHORT, closed="13:00"), _row(_REGULAR)]).encode())

    result = read_paper_us_calendar(_REGULAR, _WEEKEND, get_authorized=fetch)
    assert urls == [_PAPER + "?start=2026-11-25&end=2026-11-28&date_type=TRADING"]
    assert result.problem is None
    assert result.received_at == datetime(2026, 11, 27, 13, 43, tzinfo=UTC)
    assert result.received_at is not None
    assert result.received_at.tzinfo is UTC
    assert [session.trading_date for session in result.sessions] == [_REGULAR, _SHORT]
    regular, short = result.sessions
    assert regular.open_at.time() == time(9, 30)
    assert regular.close_at.time() == time(16)
    assert regular.shortened is False
    assert short.open_at == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert short.close_at == datetime(2026, 11, 27, 18, tzinfo=UTC)
    assert short.close_at.tzinfo == _NY
    assert short.shortened is True
    assert result.session(_SHORT) is short
    assert result.is_open(_REGULAR) is True
    assert result.is_open(_HOLIDAY) is False
    assert result.is_open(_SHORT) is True
    assert result.is_open(_WEEKEND) is False
    assert result.is_open(date(2026, 11, 24)) is None
    assert result.is_open(date(2026, 11, 29)) is None


def test_one_day_query_uses_daylight_time_for_market_clock_not_receipt_timezone():
    session = date(2026, 7, 8)
    urls = []

    def get(url):
        urls.append(url)
        return AlpacaCalendarResponse(
            url,
            200,
            datetime(2026, 7, 8, 13, tzinfo=UTC),
            json.dumps([_row(session)]).encode(),
            True,
        )

    reading = read_paper_us_calendar(session, session, get_authorized=get)
    assert len(urls) == 1
    assert urls[0].split("?")[0] == _PAPER
    assert parse_qs(urlsplit(urls[0]).query) == {
        "start": ["2026-07-08"],
        "end": ["2026-07-08"],
        "date_type": ["TRADING"],
    }
    value = reading.session(session)
    assert value is not None
    assert value.open_at == datetime(2026, 7, 8, 13, 30, tzinfo=UTC)
    assert value.close_at == datetime(2026, 7, 8, 20, tzinfo=UTC)


def test_only_complete_terminal_empty_exact_trading_query_proves_closure():
    reading = _read(b"[]", start=_HOLIDAY)
    assert reading.problem is None
    assert reading.is_open(_HOLIDAY) is False
    assert reading.session(_HOLIDAY) is None
    assert reading.is_open(_REGULAR) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"status_code": 206},
        {"status_code": 302},
        {"status_code": 401},
        {"status_code": 403},
        {"status_code": 429},
        {"status_code": 503},
        {"complete": False},
        {"body": b""},
        {"body": b" " * 16384 + b"[]"},
        {"received_at": _AT.replace(tzinfo=None)},
        {"response_url": "https://data.alpaca.markets/v2/calendar"},
        {"response_url": _PAPER + "?start=2026-11-26&end=2026-11-26&date_type=SETTLEMENT"},
    ],
)
def test_unverified_http_metadata_never_turns_empty_body_into_market_closure(changes):
    reading = read_paper_us_calendar(
        _HOLIDAY,
        _HOLIDAY,
        get_authorized=lambda url: replace(_fixture(url, b"[]"), **changes),
    )
    assert reading.problem == "unverified_response"
    assert reading.received_at is None
    assert reading.is_open(_HOLIDAY) is None


@pytest.mark.parametrize(
    "response",
    [
        b"{}",
        b"null",
        b"[",
        b'{"calendar":[]}',
        b"[{}]",
        b"[\xff]",
        b'[{"date":"2026-11-27","date":"2026-11-27","open":"09:30","close":"13:00"}]',
        json.dumps([_row(_REGULAR)]).encode(),
        json.dumps([_row(_SHORT), _row(_SHORT)]).encode(),
        json.dumps([{**_row(_SHORT), "date": "20261127"}]).encode(),
        json.dumps([_row(_SHORT, opened="9:30")]).encode(),
        json.dumps([_row(_SHORT, closed="1300")]).encode(),
        json.dumps([_row(_SHORT, closed="13:00:00")]).encode(),
        json.dumps([_row(_SHORT, closed="16:00-05:00")]).encode(),
        json.dumps([_row(_SHORT, closed="09:30")]).encode(),
        json.dumps([_row(_SHORT, opened="04:00", closed="20:00")]).encode(),
        json.dumps([_row(_SHORT, closed=None)]).encode(),
    ],
)
def test_invalid_exact_date_or_regular_row_leaves_even_empty_dates_unknown(response):
    result = _read(response)
    assert result.problem == "invalid_calendar"
    assert result.is_open(_SHORT) is None
    assert result.session(_SHORT) is None


def test_duplicate_wrong_bound_or_invalid_layout_does_not_prove_other_days_closed():
    body = json.dumps([_row(_REGULAR), _row(date(2026, 11, 29))]).encode()
    result = _read(body, start=_REGULAR, end=_WEEKEND)
    assert result.is_open(_REGULAR) is None
    assert result.is_open(_HOLIDAY) is None


def test_network_failure_is_unknown_and_never_retried_or_falls_back():
    requests = []

    def fail(url):
        requests.append(url)
        raise OSError("unreadable receipt with sensitive details")

    reading = read_paper_us_calendar(_SHORT, _SHORT, get_authorized=fail)
    assert len(requests) == 1
    assert reading.problem == "transport_error"
    assert reading.is_open(_SHORT) is None
    assert "sensitive details" not in repr(reading)


@pytest.mark.parametrize(
    ("first", "last"),
    [
        (date(1969, 12, 31), date(1970, 1, 1)),
        (date(2029, 12, 31), date(2030, 1, 1)),
        (date(2026, 11, 1), date(2026, 12, 2)),
    ],
)
def test_date_not_in_provider_coverage_or_operational_window_does_not_call_client(first, last):
    def forbidden(_url):
        pytest.fail("unsupported query must not call the transport")

    result = read_paper_us_calendar(first, last, get_authorized=forbidden)
    assert result.problem == "unsupported_window"
    assert result.is_open(first) is None


@pytest.mark.parametrize("day", [date(1970, 1, 1), date(2029, 12, 31)])
def test_inclusive_provider_coverage_boundaries_are_not_off_by_one(day):
    assert _read(b"[]", start=day).is_open(day) is False


def test_datetime_or_reversed_query_not_accepted_as_real_dates():
    def forbidden(_url):
        pytest.fail("incorrect date must not call the transport")

    with pytest.raises(ValueError, match="requires ordered NY dates"):
        read_paper_us_calendar(_SHORT, _REGULAR, get_authorized=forbidden)
    with pytest.raises(ValueError, match="requires ordered NY dates"):
        read_paper_us_calendar(_AT, _SHORT, get_authorized=forbidden)
