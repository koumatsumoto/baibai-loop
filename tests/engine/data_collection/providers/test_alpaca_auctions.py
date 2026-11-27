"""Offline tests using complete native JSON bytes, not flattened closing prices."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import pytest

from baibai_engine.data_collection.providers.alpaca_auctions import (
    NasdaqOfficialCloseQuery,
    SipAuctionReceipt,
    read_nasdaq_official_close,
)

DAY = date(2026, 6, 2)
CLOSE = datetime(2026, 6, 2, 20, tzinfo=UTC)
QUERY = NasdaqOfficialCloseQuery("AAPL", CLOSE - timedelta(minutes=5), CLOSE + timedelta(hours=1))
RECEIVED = datetime(2026, 6, 3, 1, tzinfo=UTC)
CUTOFF = RECEIVED + timedelta(seconds=1)

# P/M is an official value of a different venue; Q/6 is not the Q/M event.
BODY = b"""{
  "auctions":[{"d":"2026-06-02","o":null,"c":[
    {"x":"P","c":"M","p":125.01,"s":5,"t":"2026-06-02T20:00:00.040004Z"},
    {"x":"Q","c":"6","p":999.20,"s":100,"t":"2026-06-02T20:00:00.219341341Z"},
    {"x":"Q","c":"M","p":123.456789012345,"s":0,"t":"2026-06-02T20:00:00.221350249Z"}
  ]}],
  "next_page_token":null,
  "symbol":"AAPL"
}"""


def _receipt(body: bytes = BODY, query: NasdaqOfficialCloseQuery = QUERY) -> SipAuctionReceipt:
    return SipAuctionReceipt(
        request_url=query.url(),
        response_url=query.url(),
        status_code=200,
        received_at=RECEIVED,
        body_sha256=sha256(body).hexdigest(),
        complete=True,
        redirected=False,
        body=body,
    )


def _read(receipt: SipAuctionReceipt):
    return read_nasdaq_official_close(
        QUERY,
        receipt,
        listing_venue="XNAS",
        session_et=DAY,
        session_close_at=CLOSE,
        cutoff_at=CUTOFF,
    )


def test_relayed_xnas_official_value_is_exact_and_not_same_auction_trade() -> None:
    result = _read(_receipt())
    assert result.symbol == "AAPL"
    assert result.venue == "XNAS"
    assert result.session_et == DAY
    assert result.price_usd == Decimal("123.456789012345")
    assert result.price_unit == "USD_per_share"
    assert result.basis == "regular_session_unadjusted"
    assert result.origin == "alpaca_sip_nasdaq_official_close"
    assert (result.exchange_code, result.sale_condition) == ("Q", "M")
    assert result.observed_at == RECEIVED
    assert result.event_at_rfc3339 == "2026-06-02T20:00:00.221350249Z"
    assert result.source_json_pointer == "/auctions/0/c/2"
    assert result.native_response_sha256 == sha256(BODY).hexdigest()
    assert result.source_url == (
        "https://data.alpaca.markets/v2/stocks/AAPL/auctions?"
        "start=2026-06-02T19%3A55%3A00Z&end=2026-06-02T21%3A00%3A00Z"
        "&limit=100&feed=sip&asof=-&currency=USD"
    )


def test_shortened_ny_session_uses_its_fixed_actual_close_and_literal_class_label() -> None:
    # 13:00 New York in winter (18:00 UTC) is a synthetic shortened session.
    day = date(2026, 11, 27)
    close = datetime(2026, 11, 27, 18, tzinfo=UTC)
    query = NasdaqOfficialCloseQuery("GOOGL", close, close + timedelta(minutes=15))
    body = json.dumps(
        {
            "auctions": [
                {"d": str(day), "c": [{"x": "Q", "c": "M", "p": 314, "t": "2026-11-27T18:00:00Z"}]}
            ],
            "next_page_token": None,
            "symbol": "GOOGL",
            "feed": "sip",
            "currency": "USD",
        }
    ).encode()
    observed = close + timedelta(hours=2)
    result = read_nasdaq_official_close(
        query,
        replace(_receipt(body, query), received_at=observed),
        listing_venue="XNAS",
        session_et=day,
        session_close_at=close,
        cutoff_at=observed,
    )
    assert (result.symbol, result.session_et, result.price_usd) == ("GOOGL", day, Decimal(314))
    with pytest.raises(ValueError, match="complete single-session"):
        read_nasdaq_official_close(
            query,
            replace(_receipt(body.replace(b"GOOGL", b"GOOG"), query), received_at=observed),
            listing_venue="XNAS",
            session_et=day,
            session_close_at=close,
            cutoff_at=observed,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"status_code": 206},
        {"status_code": True},
        {"complete": False},
        {"complete": None},
        {"redirected": True},
        {"redirected": None},
        {"request_url": QUERY.url().replace("feed=sip", "feed=iex")},
        {"response_url": QUERY.url().replace("AAPL", "GOOGL")},
        {"request_url": QUERY.url() + "&feed=iex", "response_url": QUERY.url() + "&feed=iex"},
        {"request_url": QUERY.url().replace("currency=USD", "currency=JPY")},
        {"request_url": QUERY.url().replace("asof=-", "asof=2026-06-02")},
        {"request_url": QUERY.url().replace("https://", "http://")},
        {"request_url": QUERY.url().replace("v2/stocks/AAPL/auctions", "v2/stocks/AAPL/bars")},
        {"request_url": QUERY.url().replace("data.alpaca.markets", "example.invalid")},
    ],
)
def test_scope_or_transport_does_not_follow_urls_or_infer_receipt_completion(changes) -> None:
    with pytest.raises(ValueError, match="complete single-hop"):
        _read(replace(_receipt(), **changes))


@pytest.mark.parametrize(
    ("venue", "day", "close", "receive", "cutoff"),
    [
        ("XNYS", DAY, CLOSE, RECEIVED, CUTOFF),
        ("XNAS", DAY + timedelta(days=1), CLOSE, RECEIVED, CUTOFF),
        ("XNAS", DAY, CLOSE - timedelta(minutes=6), RECEIVED, CUTOFF),
        ("XNAS", DAY, QUERY.end, RECEIVED, CUTOFF),
        ("XNAS", DAY, CLOSE, QUERY.end - timedelta(microseconds=1), CUTOFF),
        ("XNAS", DAY, CLOSE, CUTOFF + timedelta(microseconds=1), CUTOFF),
        ("XNAS", DAY, CLOSE, RECEIVED, CUTOFF.replace(tzinfo=None)),
    ],
)
def test_scope_requires_covered_session_and_actual_receipt_at_cutoff(
    venue, day, close, receive, cutoff
):
    with pytest.raises(ValueError, match=r"closed session|confirmed XNAS session|timezone-aware"):
        read_nasdaq_official_close(
            QUERY,
            replace(_receipt(), received_at=receive),
            listing_venue=venue,
            session_et=day,
            session_close_at=close,
            cutoff_at=cutoff,
        )


def test_no_new_york_date_rollover_inside_one_day_scope() -> None:
    query = NasdaqOfficialCloseQuery("AAPL", QUERY.start, QUERY.end + timedelta(hours=8))
    observed = query.end + timedelta(seconds=1)
    with pytest.raises(ValueError, match="closed session"):
        read_nasdaq_official_close(
            query,
            replace(_receipt(query=query), received_at=observed),
            listing_venue="XNAS",
            session_et=DAY,
            session_close_at=CLOSE,
            cutoff_at=observed,
        )


def test_native_body_bytes_must_match_the_digest() -> None:
    with pytest.raises(ValueError, match="digest differs"):
        _read(replace(_receipt(), body=BODY + b"\n"))


@pytest.mark.parametrize(
    ("source", "replacement", "message"),
    [
        (b'"next_page_token":null,', b"", "complete single-session"),
        (b'"next_page_token":null', b'"next_page_token":"another-page"', "complete single-session"),
        (b'"symbol":"AAPL"', b'"symbol":"GOOG"', "complete single-session"),
        (b'"symbol":"AAPL"', b'"symbol":"AAPL", "currency":"JPY"', "complete single-session"),
        (b'"symbol":"AAPL"', b'"symbol":"AAPL", "feed":"iex"', "complete single-session"),
        (b'"d":"2026-06-02"', b'"d":"2026-06-03"', "complete single-session"),
        (b'"x":"Q","c":"M"', b'"x":"P","c":"M"', "exactly one"),
        (b'"x":"Q","c":"6"', b'"x":"Q","c":"M"', "exactly one"),
        (b'"x":"Q","c":"M"', b'"x":"Q","c":"M","x":"P"', "invalid SIP auction JSON"),
        (b'"x":"Q","c":"M"', b'"x":3,"c":"M"', "invalid SIP closing-event codes"),
        (b'"p":123.456789012345', b'"p":NaN', "invalid SIP auction JSON"),
        (b'"p":123.456789012345', b'"p":Infinity', "invalid SIP auction JSON"),
        (b'"p":123.456789012345', b'"p":true', "clock or price"),
        (b'"p":123.456789012345', b'"p":"123.456789012345"', "clock or price"),
        (b'"p":123.456789012345', b'"p":-22', "clock or price"),
        (b'"p":123.456789012345', b'"p":0', "clock or price"),
        (b'"p":123.456789012345', b'"p":null', "clock or price"),
        (
            b"20:00:00.221350249Z",
            b"19:59:59.999999999Z",
            "clock or price",
        ),
        (
            b"20:00:00.221350249Z",
            b"20:00:00.221350249",
            "fixed UTC timestamp",
        ),
    ],
)
def test_insufficient_or_ambiguous_native_evidence_is_not_a_close(
    source, replacement, message
) -> None:
    assert source in BODY
    with pytest.raises(ValueError, match=message):
        _read(_receipt(BODY.replace(source, replacement, 1)))


def test_event_nanosecond_order_matches_the_fixed_close_without_rounding() -> None:
    precise_close = CLOSE.replace(microsecond=221350)
    # First six digits are equal: this is 249 ns after the fixed close.
    result = read_nasdaq_official_close(
        QUERY,
        _receipt(),
        listing_venue="XNAS",
        session_et=DAY,
        session_close_at=precise_close,
        cutoff_at=CUTOFF,
    )
    assert result.event_at_rfc3339.endswith(".221350249Z")
    with pytest.raises(ValueError, match="clock or price"):
        read_nasdaq_official_close(
            QUERY,
            _receipt(BODY.replace(b".221350249Z", b".221349999Z")),
            listing_venue="XNAS",
            session_et=DAY,
            session_close_at=precise_close,
            cutoff_at=CUTOFF,
        )


@pytest.mark.parametrize(
    ("timestamp", "accepted"),
    [
        ("2026-06-02T20:59:59.999999999Z", True),
        ("2026-06-02T21:00:00Z", True),
        ("2026-06-02T21:00:00.000000001Z", False),
    ],
)
def test_event_query_end_is_inclusive_without_nanosecond_rounding(timestamp, accepted):
    body = BODY.replace(b"2026-06-02T20:00:00.221350249Z", timestamp.encode())
    if accepted:
        assert _read(_receipt(body)).event_at_rfc3339 == timestamp
    else:
        with pytest.raises(ValueError, match="clock or price"):
            _read(_receipt(body))
