"""The scheduled path uses the previous closed session's exact, pre-close Alpaca pin."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from httpx2 import ByteStream, ConnectError, MockTransport, Response

from baibai_engine.data_collection.providers.alpaca import MAX_SYMBOLS
from baibai_engine.data_collection.providers.alpaca_assets import AlpacaAssetsReceipt
from baibai_engine.data_collection.providers.alpaca_http import AlpacaCredentials
from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded
from baibai_engine.machine_analysis.observations.stocks.alpaca_listing_input import (
    prepare_alpaca_research_inventory,
)
from baibai_jobs.integration.us_daily_handoff import (
    receive_us_daily_calendar,
    receive_us_daily_prices,
)
from baibai_jobs.integration.us_daily_prices import latest_closed_us_session

_FRI = date(2026, 11, 27)
_MON = date(2026, 11, 30)
_SHORT_CLOSE = datetime(2026, 11, 27, 18, tzinfo=UTC)
_MONDAY_SLOT = datetime(2026, 11, 30, 13, 43, tzinfo=UTC)
_CREDS = AlpacaCredentials("fixture-paper", "fixture-secret")
_ATTEMPT = UUID(int=951)
_FIXTURE_SYMBOLS = MAX_SYMBOLS * 2 + 2
_NY = ZoneInfo("America/New_York")


def _fixed_pin(*, observed=_SHORT_CLOSE - timedelta(minutes=1)):
    rows = [
        {
            "id": str(UUID(int=index + 1)),
            "class": "us_equity",
            "exchange": "NASDAQ",
            "symbol": f"A{index:03d}",
            "status": "active",
            "tradable": False,
        }
        for index in range(_FIXTURE_SYMBOLS)
    ]
    return prepare_alpaca_research_inventory(
        AlpacaAssetsReceipt(observed, json.dumps(rows).encode())
    ).current


def _response(request, value, *, status=200, retry_after=None):
    body = json.dumps(value).encode()
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return Response(status, request=request, headers=headers, stream=ByteStream(body))


def _daily_rows(symbols, *, cursor=None):
    native = {"t": "2026-11-27T05:00:00Z", "o": 10, "h": 11, "l": 9, "c": 10, "v": 42}
    return {"bars": {symbol: [native] for symbol in symbols}, "next_page_token": cursor}


def _run(reply, *, observed=_SHORT_CLOSE - timedelta(minutes=1), now=_MONDAY_SLOT, new_batch=None):
    requests = []
    sleeps = []
    elapsed = 0.0

    def wait(seconds):
        nonlocal elapsed
        if new_batch:
            new_batch()
        sleeps.append(seconds)
        elapsed += seconds

    def get(request):
        assert request.method == "GET"
        parts = urlsplit(str(request.url))
        query = parse_qs(parts.query)
        if parts.netloc == "paper-api.alpaca.markets":
            assert parts.path == "/v2/calendar"
            today = now.astimezone(_NY).date()
            assert query["start"] == [(today - timedelta(days=7)).isoformat()]
            assert query["end"] == [today.isoformat()]
            requests.append(("calendar", elapsed, None))
            rows = [
                {"date": str(day), "open": "09:30", "close": closed}
                for day, closed in ((_FRI, "13:00"), (_MON, "16:00"))
                if day <= today
            ]
            return _response(request, rows)
        assert (parts.netloc, parts.path) == ("data.alpaca.markets", "/v2/stocks/bars")
        assert query["start"] == query["end"] == [_FRI.isoformat()]
        assert query["feed"] == ["sip"]
        assert query["adjustment"] == ["raw"]
        assert query["asof"] == ["-"]
        symbols = query["symbols"][0].split(",")
        page = query.get("page_token")
        requests.append((symbols, elapsed, page))
        return reply(request, symbols, page, len(requests) - 1)

    with MockTransport(get) as transport:
        calendar, wire = receive_us_daily_calendar(
            credentials=_CREDS, transport=transport, clock=lambda: now
        )
        assert wire is not None
        result = receive_us_daily_prices(
            _fixed_pin(observed=observed),
            calendar=calendar,
            credentials=_CREDS,
            transport=transport,
            clock=lambda: now,
            attempt_id=_ATTEMPT,
            expected_session=_FRI,
            wait=wait,
            monotonic_clock=lambda: elapsed,
        )
    return calendar, result, requests, sleeps


@pytest.mark.parametrize("now", [_MONDAY_SLOT, datetime(2026, 11, 28, 14, 43, tzinfo=UTC)])
def test_closed_short_session_pin_survives_open_morning_and_closed_weekend(now):
    def reply(request, symbols, page, number):
        assert page is None
        if number == 1:
            return _response(request, _daily_rows(symbols[1:]))
        if number == 2:
            return _response(request, {}, status=429, retry_after="2")
        return _response(request, _daily_rows(symbols))

    calendar, collection, requests, sleeps = _run(reply, now=now)
    session = latest_closed_us_session(calendar, cutoff=now)
    assert session is not None
    assert session.shortened
    assert session.close_at == _SHORT_CLOSE
    assert collection is not None
    assert collection.session_close_at == _SHORT_CLOSE
    assert (collection.plan.listing_session, collection.plan.price_session) == (_FRI, _FRI)
    assert [len(entry[0]) for entry in requests[1:]] == [20, 20, 20, 2]
    assert requests[2][0] == requests[3][0]
    assert [entry[1] for entry in requests[1:]] == pytest.approx([0, 0.4, 2.4, 2.8])
    assert sleeps == pytest.approx([0.4, 2, 0.4])
    assert len(collection.plan.targets) == _FIXTURE_SYMBOLS
    assert len(collection.observations) == _FIXTURE_SYMBOLS - 1
    first, second, last = collection.receipts
    assert first.coverage.missing == (("A000", _FRI),)
    assert second.capture is not None
    assert second.capture.request_id.endswith("-2-retry")
    assert len(last.coverage.received) == 2
    assert not any(receipt.coverage.unverified for receipt in collection.receipts)


def test_retry_a_transient_page_by_restarting_the_batch_with_a_new_identity():
    def reply(request, symbols, page, number):
        if number == 1:
            return _response(request, _daily_rows(symbols[:7], cursor="continue"))
        if number == 2:
            assert page == ["continue"]
            raise ConnectError("fixture temporary transport issue", request=request)
        assert page is None
        return _response(request, _daily_rows(symbols))

    _, collection, requests, sleeps = _run(reply)
    assert collection is not None
    assert collection.fully_observed
    assert requests[1][0] == requests[3][0]
    assert sleeps[:2] == pytest.approx([0.4, 1])
    capture = collection.receipts[0].capture
    assert capture is not None
    assert capture.request_id.endswith("-1-retry")
    assert len(capture.pages) == 1


@pytest.mark.parametrize(
    ("status", "retry_after"), [(401, None), (403, None), (429, "120"), (503, "120")]
)
def test_never_retry_entitlement_or_shorten_provider_wait(status, retry_after):
    _, collection, requests, sleeps = _run(
        lambda request, *args: _response(request, {}, status=status, retry_after=retry_after)
    )
    assert len(requests) == 2
    assert sleeps == []
    assert collection is not None
    assert [receipt.coverage.failure for receipt in collection.receipts] == [
        "http_status",
        "not_attempted",
        "not_attempted",
    ]
    assert (
        sum(len(receipt.coverage.unverified) for receipt in collection.receipts) == _FIXTURE_SYMBOLS
    )
    assert not any(receipt.coverage.missing for receipt in collection.receipts)


def test_job_cutoff_does_not_discard_previously_received_bars():
    def deadline():
        raise AcquisitionDeadlineExceeded("fixture job deadline")

    _, collection, requests, sleeps = _run(
        lambda request, symbols, *_: _response(request, _daily_rows(symbols)),
        new_batch=deadline,
    )
    assert len(requests) == 2
    assert not sleeps
    assert collection is not None
    assert len(collection.observations) == MAX_SYMBOLS
    assert (
        sum(len(receipt.coverage.unverified) for receipt in collection.receipts) == MAX_SYMBOLS + 2
    )


def test_retry_after_on_last_attempt_still_delays_the_next_batch():
    def reply(request, symbols, page, number):
        if number <= 2:
            return _response(request, {}, status=503, retry_after=str(number))
        return _response(request, _daily_rows(symbols))

    _, collection, requests, sleeps = _run(reply)
    assert collection is not None
    assert [entry[1] for entry in requests[1:]] == pytest.approx([0, 1, 3, 3.4])
    assert sleeps == pytest.approx([1, 2, 0.4])
    assert collection.receipts[0].coverage.failure == "http_status"
    assert len(collection.observations) == _FIXTURE_SYMBOLS - MAX_SYMBOLS


def test_fresh_post_close_snapshot_cannot_attest_prior_price_identity():
    _, collection, requests, sleeps = _run(
        lambda *_: pytest.fail("unexpected SIP GET"),
        observed=_SHORT_CLOSE + timedelta(seconds=1),
    )
    assert collection is None
    assert len(requests) == 1
    assert not sleeps
