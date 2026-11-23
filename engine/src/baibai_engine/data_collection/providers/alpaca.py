"""Bounded wire capture of Alpaca's US stock daily bars, independent of persistence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal
from urllib.parse import urlencode

from baibai_engine.primitives.json import canonical_json

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
MAX_SYMBOLS = 20
MAX_PAGES = 16
PAGE_LIMIT = 10_000  # Per page across all symbols, not per symbol.

type BarsFailure = Literal[
    "transport_error",
    "http_status",
    "invalid_envelope",
    "scope_mismatch",
    "invalid_cursor",
    "pagination_cycle",
    "page_limit",
]


@dataclass(frozen=True, slots=True)
class DailyBarsQuery:
    """One IEX/USD scope. Alpaca's asof identifies symbols, not data availability."""

    symbols: tuple[str, ...]
    start: date
    end: date
    symbol_asof: date | Literal["-"]
    adjustment: Literal["raw", "split"]

    def __post_init__(self) -> None:
        if (
            type(self.symbols) is not tuple
            or not 1 <= len(self.symbols) <= MAX_SYMBOLS
            or any(
                type(symbol) is not str
                or re.fullmatch(r"[A-Z][A-Z0-9]{0,11}(?:\.[A-Z0-9]{1,4})?", symbol) is None
                for symbol in self.symbols
            )
            or len(set(self.symbols)) != len(self.symbols)
        ):
            raise ValueError("daily bars require distinct US stock symbol labels")
        if type(self.start) is not date or type(self.end) is not date or self.start > self.end:
            raise ValueError("daily bars require explicit ordered interval dates")
        if type(self.symbol_asof) is not date and self.symbol_asof != "-":
            raise ValueError("select a fixed symbol date or disable Alpaca symbol mapping")
        if self.adjustment not in ("raw", "split"):
            raise ValueError("daily bars require explicit raw or split adjustment")

    def url(self, *, page_token: str | None = None) -> str:
        # A cursor is opaque query data. It never supplies a host, path or another parameter.
        params = {
            "symbols": ",".join(self.symbols),
            "timeframe": "1Day",
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "limit": str(PAGE_LIMIT),
            "adjustment": self.adjustment,
            "asof": self.symbol_asof if self.symbol_asof == "-" else self.symbol_asof.isoformat(),
            "feed": "iex",
            "currency": "USD",
            "sort": "asc",
        }
        if page_token is not None:
            if not _valid_token(page_token):
                raise ValueError("invalid Alpaca continuation token")
            params["page_token"] = page_token
        return f"{BARS_URL}?{urlencode(params)}"


@dataclass(frozen=True, slots=True)
class BarsResponse:
    """The injected transport's final status, actual receipt time and undecoded content."""

    status_code: int
    received_at: datetime
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("invalid Alpaca HTTP status")
        if type(self.received_at) is not datetime or self.received_at.utcoffset() is None:
            raise ValueError("Alpaca receipt time must be timezone-aware")
        if type(self.body) is not bytes:
            raise ValueError("Alpaca receipt requires original response bytes")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class BarsPage:
    request_id: str
    page_number: int
    request_url: str = field(repr=False)
    response: BarsResponse
    returned_symbols: tuple[str, ...] | None
    body_sha256: str = field(init=False)
    receipt_id: str = field(init=False)

    def __post_init__(self) -> None:
        body_sha = hashlib.sha256(self.response.body).hexdigest()
        identity = [
            "alpaca-daily-bars-page-v1",
            self.request_id,
            self.page_number,
            self.request_url,
            self.response.status_code,
            self.response.received_at,
            body_sha,
        ]
        object.__setattr__(self, "body_sha256", body_sha)
        object.__setattr__(
            self, "receipt_id", hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        )


@dataclass(frozen=True, slots=True)
class BarsCapture:
    query: DailyBarsQuery
    request_id: str
    pages: tuple[BarsPage, ...]
    failure: BarsFailure | None

    @property
    def complete(self) -> bool:
        """The wire chain ended normally, not that all requested instruments/trading days exist."""
        return bool(self.pages) and self.failure is None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous JSON object")
        result[key] = value
    return result


def _non_json_number(_: str) -> None:
    raise ValueError("invalid JSON number")


def _page_envelope(body: bytes) -> tuple[tuple[str, ...], object]:
    value = json.loads(
        body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_non_json_number
    )
    if not isinstance(value, dict) or not isinstance(bars := value.get("bars"), dict):
        raise ValueError("Alpaca bars are not grouped by returned symbol")
    if any(
        not key
        or not key.isprintable()
        or not isinstance(rows, list)
        or any(not isinstance(row, dict) for row in rows)
        for key, rows in bars.items()
    ):
        raise ValueError("invalid Alpaca bar group")
    if sum(map(len, bars.values())) > PAGE_LIMIT:
        raise ValueError("Alpaca page exceeds its requested row limit")
    # Distinguish explicit null from an omitted field. No name-mapping or row projection here.
    token: object = value.get("next_page_token", _OMITTED)
    return tuple(bars), token


_OMITTED = object()


def _valid_token(token: object) -> bool:
    return isinstance(token, str) and bool(token.strip()) and token.isprintable()


def capture_daily_bars(
    query: DailyBarsQuery,
    *,
    request_id: str,
    transport: Callable[[str], BarsResponse],
    max_pages: int = MAX_PAGES,
) -> BarsCapture:
    """Invoke the supplied transport once per page; keep failures without retry or fallback.

    Reuse the fixed receipt to reprocess it. A later network attempt must use its own ID.
    The transport may raise OSError if no response was received.
    """
    if (
        type(request_id) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", request_id) is None
    ):
        raise ValueError("a fixed Alpaca attempt identity is required")
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
        raise ValueError("invalid Alpaca page bound")

    pages: list[BarsPage] = []
    seen: set[str] = set()
    cursor: str | None = None
    for page_number in range(1, max_pages + 1):
        url = query.url(page_token=cursor)
        try:
            response = transport(url)
        except OSError:
            return BarsCapture(query, request_id, tuple(pages), "transport_error")
        symbols = None
        next_cursor: object = _OMITTED
        failure: BarsFailure | None = None
        if response.status_code != 200:
            failure = "http_status"
        else:
            try:
                symbols, next_cursor = _page_envelope(response.body)
            except (ValueError, RecursionError):
                failure = "invalid_envelope"
        pages.append(BarsPage(request_id, page_number, url, response, symbols))
        if failure is not None:
            return BarsCapture(query, request_id, tuple(pages), failure)
        if (
            query.symbol_asof == "-"
            and symbols is not None
            and not set(symbols) <= set(query.symbols)
        ):
            return BarsCapture(query, request_id, tuple(pages), "scope_mismatch")
        if next_cursor is None:
            return BarsCapture(query, request_id, tuple(pages), None)
        if not isinstance(next_cursor, str) or not _valid_token(next_cursor):
            return BarsCapture(query, request_id, tuple(pages), "invalid_cursor")
        if next_cursor in seen:
            return BarsCapture(query, request_id, tuple(pages), "pagination_cycle")
        seen.add(next_cursor)
        cursor = next_cursor
    return BarsCapture(query, request_id, tuple(pages), "page_limit")
