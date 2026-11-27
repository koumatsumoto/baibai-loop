"""Read a single fixed SIP auction receipt for the Nasdaq official closing value."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.alpaca import (
    decode_alpaca_json,
    is_alpaca_symbol,
)
from baibai_engine.primitives.numbers import finite_decimal

_ET = ZoneInfo("America/New_York")
_AUCTIONS_BASE = "https://data.alpaca.markets/v2/stocks"
_EVENT_CLOCK = re.compile(
    r"(?P<seconds>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?Z"
)


def _utc(stamp: object) -> datetime:
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError("SIP auction clocks require timezone-aware instants")
    return stamp.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class NasdaqOfficialCloseQuery:
    """One literal ticker and a UTC window around one independently confirmed close."""

    symbol: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not is_alpaca_symbol(self.symbol):
            raise ValueError("SIP auction request requires one literal Alpaca symbol")
        start, end = _utc(self.start), _utc(self.end)
        if start >= end or start.microsecond or end.microsecond:
            raise ValueError("SIP auction request requires ordered whole-second UTC bounds")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def url(self) -> str:
        params = {
            "start": self.start.isoformat().removesuffix("+00:00") + "Z",
            "end": self.end.isoformat().removesuffix("+00:00") + "Z",
            "limit": "100",
            "feed": "sip",
            "asof": "-",
            "currency": "USD",
        }
        return f"{_AUCTIONS_BASE}/{self.symbol}/auctions?{urlencode(params)}"


@dataclass(frozen=True, slots=True)
class SipAuctionReceipt:
    """Trusted capture metadata accompanies the original content, not a regenerated body."""

    request_url: str
    response_url: str
    status_code: int
    received_at: datetime
    body_sha256: str
    complete: bool
    redirected: bool
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class NasdaqOfficialClose:
    """An exchange-originated official observation relayed by Alpaca SIP, not a fill."""

    symbol: str
    session_et: date
    price_usd: Decimal
    source_url: str
    observed_at: datetime
    native_response_sha256: str
    source_json_pointer: str
    event_at_rfc3339: str
    origin: Literal["alpaca_sip_nasdaq_official_close"] = "alpaca_sip_nasdaq_official_close"
    venue: Literal["XNAS"] = "XNAS"
    basis: Literal["regular_session_unadjusted"] = "regular_session_unadjusted"
    price_unit: Literal["USD_per_share"] = "USD_per_share"
    exchange_code: Literal["Q"] = "Q"
    sale_condition: Literal["M"] = "M"


def _instant_key(stamp: datetime) -> tuple[datetime, int]:
    return stamp.replace(microsecond=0), stamp.microsecond * 1000


def _event_key(native: str) -> tuple[datetime, int]:
    if (match := _EVENT_CLOCK.fullmatch(native)) is None:
        raise ValueError("SIP auction event requires a fixed UTC timestamp")
    # Alpaca emits up to nine fractional digits. Compare without truncating and keep the
    # exact provider timestamp, including its original precision, in the result.
    second = datetime.fromisoformat(match["seconds"] + "+00:00")
    return second, int((match["fraction"] or "").ljust(9, "0"))


def _official_event(body: bytes, symbol: str, session_et: date) -> tuple[int, dict[str, object]]:
    try:
        payload = decode_alpaca_json(body)
    except (ValueError, ArithmeticError, RecursionError) as error:
        raise ValueError("invalid SIP auction JSON") from error
    if (
        not isinstance(payload, dict)
        or payload.get("symbol") != symbol
        or payload.get("currency", "USD") != "USD"
        or payload.get("feed", "sip") != "sip"
        or "next_page_token" not in payload
        or payload["next_page_token"] is not None
        or not isinstance(days := payload.get("auctions"), list)
        or len(days) != 1
        or not isinstance(day := days[0], dict)
        or day.get("d") != session_et.isoformat()
        or not isinstance(events := day.get("c"), list)
    ):
        raise ValueError("SIP auction is not a complete single-session response")
    matches: list[tuple[int, dict[str, object]]] = []
    for number, event in enumerate(events):
        if (
            not isinstance(event, dict)
            or not isinstance(exchange := event.get("x"), str)
            or len(exchange) != 1
            or not isinstance(condition := event.get("c"), str)
            or len(condition) != 1
        ):
            raise ValueError("invalid SIP closing-event codes")
        if exchange == "Q" and condition == "M":
            matches.append((number, event))
    if len(matches) != 1:
        raise ValueError("SIP auction requires exactly one Nasdaq official close (Q/M)")
    return matches[0]


def read_nasdaq_official_close(
    query: NasdaqOfficialCloseQuery,
    receipt: SipAuctionReceipt,
    *,
    listing_venue: str,
    session_et: date,
    session_close_at: datetime,
    cutoff_at: datetime,
) -> NasdaqOfficialClose:
    """Accept only the independently fixed XNAS listing and NY session; never fetch."""
    if listing_venue != "XNAS" or type(session_et) is not date:
        raise ValueError("official SIP close requires an independently confirmed XNAS session")
    close = _utc(session_close_at)
    cutoff = _utc(cutoff_at)
    received = _utc(receipt.received_at)
    if not query.start <= close < query.end <= received <= cutoff or any(
        stamp.astimezone(_ET).date() != session_et for stamp in (query.start, close, query.end)
    ):
        raise ValueError("SIP auction does not cover the closed session before cutoff")
    url = query.url()
    if (
        type(receipt.status_code) is not int
        or receipt.status_code != 200
        or receipt.complete is not True
        or receipt.redirected is not False
        or receipt.request_url != url
        or receipt.response_url != url
        or type(receipt.body) is not bytes
    ):
        raise ValueError("SIP auction requires an original, complete single-hop response")
    digest = sha256(receipt.body).hexdigest()
    if digest != receipt.body_sha256:
        raise ValueError("SIP auction original response digest differs")

    number, event = _official_event(receipt.body, query.symbol, session_et)
    clock = event.get("t")
    price_input = event.get("p")
    if (
        not isinstance(clock, str)
        or not _instant_key(close) <= _event_key(clock) <= _instant_key(query.end)
        or type(price_input) not in (int, Decimal)
        or (price := finite_decimal(price_input)) <= 0
    ):
        raise ValueError("invalid Nasdaq official close event clock or price")
    return NasdaqOfficialClose(
        symbol=query.symbol,
        session_et=session_et,
        price_usd=price,
        source_url=url,
        observed_at=received,
        native_response_sha256=digest,
        source_json_pointer=f"/auctions/0/c/{number}",
        event_at_rfc3339=clock,
    )
