"""Read an exact US regular-session window from Alpaca's Paper Trading calendar."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.alpaca import decode_alpaca_json

_URL = "https://paper-api.alpaca.markets/v2/calendar"
_NY = ZoneInfo("America/New_York")
_FIRST = date(1970, 1, 1)
_LAST = date(2029, 12, 31)
_MAX_DAYS = 31
_MAX_BODY = 16 * 1024
_CORE_OPEN = time(9, 30)
_CORE_CLOSE = time(16)


@dataclass(frozen=True, slots=True)
class AlpacaCalendarResponse:
    """The final receipt of a caller-authorized complete authenticated GET."""

    response_url: str
    status_code: int
    received_at: datetime
    body: bytes = field(repr=False)
    complete: bool


@dataclass(frozen=True, slots=True)
class USRegularSession:
    trading_date: date
    open_at: datetime
    close_at: datetime

    @property
    def shortened(self) -> bool:
        return self.close_at.time() < _CORE_CLOSE


type CalendarProblem = Literal[
    "unsupported_window", "transport_error", "unverified_response", "invalid_calendar"
]


@dataclass(frozen=True, slots=True)
class AlpacaCalendarReading:
    start: date
    end: date
    sessions: tuple[USRegularSession, ...] = ()
    received_at: datetime | None = None
    problem: CalendarProblem | None = None

    def session(self, trading_date: date) -> USRegularSession | None:
        return next((item for item in self.sessions if item.trading_date == trading_date), None)

    def is_open(self, trading_date: date) -> bool | None:
        """True for regular or short, False for a proved closure, None if unknown."""
        if self.received_at is None or not self.start <= trading_date <= self.end:
            return None
        return self.session(trading_date) is not None


def _clock(value: object) -> time:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{2}:[0-9]{2}", value) is None:
        raise ValueError("regular calendar time requires HH:MM")
    return time.fromisoformat(value)


def _sessions(body: bytes, start: date, end: date) -> tuple[USRegularSession, ...]:
    data = decode_alpaca_json(body)
    if not isinstance(data, list):
        raise ValueError("US regular-session calendar is not an array")
    sessions: dict[date, USRegularSession] = {}
    for row in data:
        if not isinstance(row, dict) or not isinstance(native_date := row.get("date"), str):
            raise ValueError("US regular-session calendar lacks a trading date")
        trading_date = date.fromisoformat(native_date)
        if trading_date.isoformat() != native_date or not start <= trading_date <= end:
            raise ValueError("US regular-session calendar date is outside the exact query")
        if trading_date in sessions:
            raise ValueError("US regular-session calendar repeats a trading date")
        opened, closed = _clock(row.get("open")), _clock(row.get("close"))
        if not _CORE_OPEN <= opened < closed <= _CORE_CLOSE:
            raise ValueError("US regular-session calendar has invalid core hours")
        # The US legacy calendar's HH:MM values are local market times, not UTC.
        sessions[trading_date] = USRegularSession(
            trading_date,
            datetime.combine(trading_date, opened, tzinfo=_NY),
            datetime.combine(trading_date, closed, tzinfo=_NY),
        )
    return tuple(sessions[trading_date] for trading_date in sorted(sessions))


def read_paper_us_calendar(
    start: date,
    end: date,
    *,
    get_authorized: Callable[[str], AlpacaCalendarResponse],
) -> AlpacaCalendarReading:
    """Read one small explicit window; prefer start=end for one session.

    The caller owns authorization and direct complete authenticated HTTPS receipt.
    An IEX/SIP Market Data credential check is not permission or proof of this
    Paper Trading route. There are no retries, redirects, alternate providers or
    credentials in this adapter. First operational use needs separate validation.
    """
    if type(start) is not date or type(end) is not date or end < start:
        raise ValueError("US calendar requires ordered NY dates")
    if not _FIRST <= start <= end <= _LAST or (end - start).days >= _MAX_DAYS:
        return AlpacaCalendarReading(start, end, problem="unsupported_window")
    params = urlencode({"start": start.isoformat(), "end": end.isoformat(), "date_type": "TRADING"})
    url = f"{_URL}?{params}"
    try:
        response = get_authorized(url)
    except OSError:
        return AlpacaCalendarReading(start, end, problem="transport_error")
    if (
        response.status_code != 200
        or response.response_url != url
        or response.complete is not True
        or not 0 < len(response.body) <= _MAX_BODY
        or response.received_at.utcoffset() is None
    ):
        return AlpacaCalendarReading(start, end, problem="unverified_response")
    try:
        sessions = _sessions(response.body, start, end)
    except (ValueError, ArithmeticError, RecursionError):
        return AlpacaCalendarReading(start, end, problem="invalid_calendar")
    return AlpacaCalendarReading(start, end, sessions, response.received_at.astimezone(UTC))
