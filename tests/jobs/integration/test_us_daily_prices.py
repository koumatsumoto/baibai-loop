from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from tests.jobs.integration.test_us_daily_plan import _CUTOFF, _FRI, _MON, _attach, _store

from baibai_engine.data_collection.providers.alpaca import MAX_SYMBOLS, BarsResponse
from baibai_engine.data_collection.providers.alpaca_calendar import (
    AlpacaCalendarResponse,
    read_paper_us_calendar,
)
from baibai_jobs.integration.us_daily_prices import collect_current_us_daily_prices

_ATTEMPT = UUID("10000000-0000-0000-0000-000000000001")
_CALENDAR_ROWS = (
    {"date": _FRI.isoformat(), "open": "09:30", "close": "13:00"},
    {"date": _MON.isoformat(), "open": "09:30", "close": "16:00"},
)


def _calendar(*, arrived=_CUTOFF, rows=_CALENDAR_ROWS, start=_FRI, end=_MON, status=200):
    return read_paper_us_calendar(
        start,
        end,
        get_authorized=lambda url: AlpacaCalendarResponse(
            url, status, arrived, json.dumps(rows).encode(), True
        ),
    )


def _payload(symbols, *, day=_FRI, next_page=None):
    native = {"t": f"{day}T05:00:00Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 42}
    return {"bars": {symbol: [native] for symbol in symbols}, "next_page_token": next_page}


def _response(body, *, status=200, arrived=_CUTOFF):
    return BarsResponse(status, arrived, json.dumps(body).encode())


def _run(current, transport, *, begun=_CUTOFF, calendar=None):
    clock = iter([begun, *([begun + timedelta(minutes=1)] * 3)])
    return collect_current_us_daily_prices(
        current,
        _calendar() if calendar is None else calendar,
        attempt_id=_ATTEMPT,
        clock=lambda: next(clock),
        transport=transport,
    )


def test_collects_whole_current_universe_through_exact_sip_batches_and_last_page():
    current = _attach(_store())
    requests = []

    def get(url):
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        assert (parts.scheme, parts.netloc, parts.path) == (
            "https",
            "data.alpaca.markets",
            "/v2/stocks/bars",
        )
        assert query["feed"] == ["sip"]
        assert query["asof"] == ["-"]
        assert query["timeframe"] == ["1Day"]
        assert query["adjustment"] == ["raw"]
        assert query["currency"] == ["USD"]
        assert query["start"] == query["end"] == [_FRI.isoformat()]
        symbols = tuple(query["symbols"][0].split(","))
        requests.append((symbols, query.get("page_token")))
        if len(requests) == 1:
            return _response(_payload(symbols[:7], next_page="next"))
        if len(requests) == 2:
            assert requests[-1] == (requests[0][0], ["next"])
            return _response(_payload(symbols[7:]))
        return _response(_payload(symbols))

    result = _run(current, get)
    assert result is not None
    assert result.fully_observed
    assert (result.plan.listing_session, result.plan.price_session) == (_MON, _FRI)
    assert [len(symbols) for symbols, _ in requests] == [MAX_SYMBOLS, MAX_SYMBOLS, MAX_SYMBOLS, 2]
    assert len(result.observations) == 2 * MAX_SYMBOLS + 2
    assert [target for target, _ in result.observations] == list(result.plan.targets)
    assert result.observations[0][0].listing_key == ("XNYS", "BRK.B")
    assert all(row.trade_date == _FRI and row.feed == "sip" for _, row in result.observations)
    assert [r.capture.request_id for r in result.receipts] == [
        f"us-daily-{_ATTEMPT}-{i}" for i in range(1, 4)
    ]


def test_same_ny_day_is_selected_only_after_its_regular_close():
    clock = datetime(2026, 11, 30, 21, 1, tzinfo=UTC)

    def get(url):
        query = parse_qs(urlsplit(url).query)
        assert query["start"] == query["end"] == [_MON.isoformat()]
        return _response(_payload(query["symbols"][0].split(","), day=_MON), arrived=clock)

    result = _run(_attach(_store()), get, begun=clock)
    assert result is not None
    assert result.fully_observed
    assert result.plan.price_session == _MON
    assert ("XNAS", "WMT") in {target.listing_key for target, _ in result.observations}
    assert len(result.observations) == 2 * MAX_SYMBOLS + 5


@pytest.mark.parametrize(
    "calendar",
    [
        _calendar(arrived=_CUTOFF + timedelta(microseconds=1)),
        _calendar(rows=()),
        _calendar(rows=_CALENDAR_ROWS[1:], start=_MON),
        _calendar(rows=_CALENDAR_ROWS[:1], end=_FRI),
        _calendar(rows=(), status=403),
    ],
)
def test_no_requests_when_calendar_is_closed_not_yet_known_or_has_no_closed_session(calendar):
    assert (
        _run(_attach(_store()), lambda _: pytest.fail("unexpected SIP GET"), calendar=calendar)
        is None
    )


def test_no_requests_when_complete_broker_scope_is_not_pinned():
    assert _run(None, lambda _: pytest.fail("unexpected SIP GET")) is None


@pytest.mark.parametrize("status", [401, 403, 429])
def test_global_entitlement_or_quota_failure_does_not_repeat_for_remaining_batches(status):
    calls = []

    def get(url):
        calls.append(url)
        return _response({}, status=status)

    result = _run(_attach(_store()), get)
    assert result is not None
    assert not result.fully_observed
    assert len(calls) == 1
    assert [r.coverage.failure for r in result.receipts] == [
        "http_status",
        "not_attempted",
        "not_attempted",
    ]
    assert not result.observations
    assert sum(len(r.coverage.unverified) for r in result.receipts) == len(result.plan.targets)
    assert not any(r.coverage.missing for r in result.receipts)


def test_complete_missing_and_failed_chain_and_valid_other_batch_remain_distinct():
    calls = []

    def get(url):
        symbols = parse_qs(urlsplit(url).query)["symbols"][0].split(",")
        calls.append(symbols)
        if len(calls) == 1:
            return _response(_payload(symbols[1:]))
        if len(calls) == 2:
            return _response({}, status=503)
        return _response(_payload(symbols))

    result = _run(_attach(_store()), get)
    assert result is not None
    assert not result.fully_observed
    first, second, last = [item.coverage for item in result.receipts]
    assert first.missing == ((calls[0][0], _FRI),)
    assert first.failure is None
    assert first.unverified == ()
    assert second.failure == "http_status"
    assert second.missing == ()
    assert second.unverified == second.batch.expected
    assert last.failure is None
    assert last.missing == ()
    assert len(result.observations) == MAX_SYMBOLS + 1


@pytest.mark.parametrize(
    "offset", [timedelta(microseconds=-1), timedelta(minutes=1, microseconds=1)]
)
def test_receipts_outside_the_actual_run_are_unverified_not_available(offset):
    def get(url):
        symbols = parse_qs(urlsplit(url).query)["symbols"][0].split(",")
        return _response(_payload(symbols), arrived=_CUTOFF + offset)

    result = _run(_attach(_store()), get)
    assert result is not None
    assert not result.fully_observed
    assert not result.observations
    assert all(
        r.coverage.failure == "invalid_capture"
        and r.coverage.unverified == r.coverage.batch.expected
        for r in result.receipts
    )
