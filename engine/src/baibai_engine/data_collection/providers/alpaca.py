"""Bounded wire capture of Alpaca's US stock daily bars, independent of persistence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
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
    "receipt_time_order",
    "scope_mismatch",
    "invalid_cursor",
    "pagination_cycle",
    "page_limit",
]
type NativeBarGroups = dict[str, list[dict[str, object]]]


class AlpacaCaptureIncomplete(ValueError):
    """A failed or absent receipt chain cannot establish an empty observation set."""


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
    max_pages: int

    @property
    def complete(self) -> bool:
        """The wire chain ended normally, not that all requested instruments/trading days exist."""
        return bool(self.pages) and self.failure is None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous JSON object")
        result[key] = value
    return result


def _non_json_number(_: str) -> None:
    raise ValueError("invalid JSON number")


def _page_envelope(body: bytes) -> tuple[NativeBarGroups, object]:
    value = json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_non_json_number,
        parse_float=Decimal,
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
    return bars, token


_OMITTED = object()


def _valid_token(token: object) -> bool:
    return isinstance(token, str) and bool(token.strip()) and token.isprintable()


@dataclass(slots=True)
class _DailyBarsChain:
    """One protocol owner for new response capture and original-receipt replay."""

    query: DailyBarsQuery
    request_id: str
    max_pages: int
    number: int = field(init=False, default=0)
    cursor: str | None = field(init=False, default=None, repr=False)
    seen: set[str] = field(init=False, default_factory=set, repr=False)
    prior_receipt: datetime | None = field(init=False, default=None)
    done: bool = field(init=False, default=False)
    failure: BarsFailure | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if (
            type(self.request_id) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", self.request_id) is None
        ):
            raise ValueError("a fixed Alpaca attempt identity is required")
        if type(self.max_pages) is not int or not 1 <= self.max_pages <= MAX_PAGES:
            raise ValueError("invalid Alpaca page bound")

    def next_url(self) -> str:
        if self.done:
            raise ValueError("Alpaca page receipt does not match the fixed capture")
        return self.query.url(page_token=self.cursor)

    def observe(self, url: str, response: BarsResponse) -> tuple[BarsPage, NativeBarGroups | None]:
        self.number += 1
        groups: NativeBarGroups | None = None
        next_cursor: object = _OMITTED
        if response.status_code != 200:
            self.failure = "http_status"
        else:
            try:
                groups, next_cursor = _page_envelope(response.body)
            except (ValueError, ArithmeticError, RecursionError):
                self.failure = "invalid_envelope"
        symbols = None if groups is None else tuple(groups)
        page = BarsPage(self.request_id, self.number, url, response, symbols)
        if self.failure is None:
            self._advance(next_cursor, symbols, response.received_at)
        self.done = self.failure is not None or next_cursor is None
        return page, groups

    def _advance(
        self, next_cursor: object, symbols: tuple[str, ...] | None, received_at: datetime
    ) -> None:
        if self.prior_receipt is not None and received_at < self.prior_receipt:
            self.failure = "receipt_time_order"
        elif (
            self.query.symbol_asof == "-"
            and symbols is not None
            and not set(symbols) <= set(self.query.symbols)
        ):
            self.failure = "scope_mismatch"
        elif next_cursor is not None:
            if not isinstance(next_cursor, str) or not _valid_token(next_cursor):
                self.failure = "invalid_cursor"
            elif next_cursor in self.seen:
                self.failure = "pagination_cycle"
            elif self.number == self.max_pages:
                self.failure = "page_limit"
            else:
                self.seen.add(next_cursor)
                self.cursor = next_cursor
        self.prior_receipt = received_at


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
    chain = _DailyBarsChain(query, request_id, max_pages)
    pages: list[BarsPage] = []
    while not chain.done:
        url = chain.next_url()
        try:
            response = transport(url)
        except OSError:
            return BarsCapture(query, request_id, tuple(pages), "transport_error", max_pages)
        page, _ = chain.observe(url, response)
        pages.append(page)
    return BarsCapture(query, request_id, tuple(pages), chain.failure, max_pages)


def read_complete_daily_bar_pages(
    capture: BarsCapture,
) -> tuple[tuple[BarsPage, NativeBarGroups], ...]:
    """Read exact JSON numbers from a fixed native capture, with no new request."""
    if not capture.complete:
        reason = capture.failure or "no_receipt"
        raise AlpacaCaptureIncomplete(f"incomplete Alpaca daily bars capture: {reason}")

    chain = _DailyBarsChain(capture.query, capture.request_id, capture.max_pages)
    result: list[tuple[BarsPage, NativeBarGroups]] = []
    for pinned in capture.pages:
        expected, groups = chain.observe(chain.next_url(), pinned.response)
        if chain.failure is not None:
            raise ValueError(f"invalid Alpaca fixed receipt chain: {chain.failure}")
        if expected != pinned or groups is None:
            raise ValueError("Alpaca page receipt does not match the fixed capture")
        result.append((pinned, groups))
    if not chain.done:
        raise ValueError("Alpaca fixed capture has no terminal receipt")
    return tuple(result)
