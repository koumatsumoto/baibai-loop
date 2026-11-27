"""Check one US session's Alpaca IEX daily observations without publishing any data."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from httpx2 import BaseTransport, HTTPTransport, Request, StreamError, TransportError
from tools.experiments.us_research_mvp import _observed_prices, _whole_identity_headers

from baibai_engine.data_collection.providers.alpaca import (
    BarsResponse,
    DailyBarsQuery,
    capture_daily_bars,
)
from baibai_engine.machine_analysis.observations.prices.alpaca_normalization import (
    normalize_alpaca_daily_bars,
)

_SYMBOLS = ("AAPL", "GOOG", "GOOGL", "NVDA")
_NY = ZoneInfo("America/New_York")
_MAX_BYTES = 128 * 1024
_TIMEOUT = {"connect": 10.0, "read": 15.0, "write": 10.0, "pool": 10.0}


def _now() -> datetime:
    return datetime.now(UTC)


def _credentials() -> dict[str, str] | None:
    headers = {
        "APCA-API-KEY-ID": os.environ.get("APCA_API_KEY_ID", ""),
        "APCA-API-SECRET-KEY": os.environ.get("APCA_API_SECRET_KEY", ""),
    }
    if not all(
        value and value.isascii() and value.isprintable() and " " not in value
        for value in headers.values()
    ):
        return None
    return headers


def _read_page(
    transport: BaseTransport,
    url: str,
    credentials: dict[str, str],
    clock: Callable[[], datetime],
) -> BarsResponse:
    request = Request(
        "GET",
        url,
        headers={"Accept": "application/json", "Accept-Encoding": "identity", **credentials},
        extensions={"timeout": dict(_TIMEOUT)},
    )
    # The low-level transport has no redirect, cookie, or proxy client policies.
    try:
        with closing(transport.handle_request(request)) as response:
            original = bytearray()
            for chunk in response.iter_raw():
                if len(original) + len(chunk) > _MAX_BYTES:
                    raise OSError("Alpaca daily response exceeds the byte bound")
                original.extend(chunk)
            received_at = clock()
            body = bytes(original)
            if response.status_code == 200:
                _whole_identity_headers({"headers": dict(response.headers)}, body)
            return BarsResponse(response.status_code, received_at, body)
    except (StreamError, TransportError, ValueError):
        # Never propagate provider messages, URLs, body text or request headers to output.
        raise OSError("Alpaca daily response could not be fully read") from None


def _result(
    expected: date | None,
    status: str,
    *,
    matched: int = 0,
    http_status: int | None = None,
    code: int = 1,
) -> int:
    http = f" http_status={http_status}" if http_status is not None else ""
    print(
        f"iex_session={expected or '-'} status={status} symbols_ok={matched}/{len(_SYMBOLS)}{http}"
    )
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Confirm the specified New York session for four approved Alpaca IEX tickers.",
        allow_abbrev=False,
    )
    parser.add_argument("--expected-date", required=True, metavar="YYYY-MM-DD")
    args = parser.parse_args(argv)
    try:
        expected = date.fromisoformat(args.expected_date)
        if expected.isoformat() != args.expected_date:
            raise ValueError("expected date must use YYYY-MM-DD")
    except ValueError:
        return _result(None, "invalid_date", code=2)

    if expected >= _now().astimezone(_NY).date():
        return _result(expected, "awaiting_ny_date", code=2)
    credentials = _credentials()
    if credentials is None:
        return _result(expected, "missing_credentials", code=2)

    # The next date boundary keeps the target NY midnight within Alpaca's inclusive
    # interval. A neighboring day's aggregate, if returned, never satisfies acceptance.
    query = DailyBarsQuery(_SYMBOLS, expected, expected + timedelta(days=1), "-", "split")
    try:
        with HTTPTransport(trust_env=False, retries=0) as transport:
            capture = capture_daily_bars(
                query,
                request_id=f"iex-accept-{uuid4()}",
                transport=lambda url: _read_page(transport, url, credentials, _now),
            )
    except (OSError, StreamError, TransportError, ValueError):
        return _result(expected, "transport_error")
    if capture.failure == "http_status":
        return _result(
            expected, capture.failure, http_status=capture.pages[-1].response.status_code
        )
    if not capture.complete:
        return _result(expected, capture.failure or "no_receipt")

    try:
        observations = normalize_alpaca_daily_bars(capture)
    except (ArithmeticError, ValueError):
        return _result(expected, "invalid_observations")
    matched = sum(
        any(row.trade_date == expected for row, _ in _observed_prices(symbol, observations)[0])
        for symbol in _SYMBOLS
    )
    if matched != len(_SYMBOLS):
        return _result(expected, "missing_symbols", matched=matched)
    return _result(expected, "accepted", matched=matched, code=0)


if __name__ == "__main__":
    raise SystemExit(main())
