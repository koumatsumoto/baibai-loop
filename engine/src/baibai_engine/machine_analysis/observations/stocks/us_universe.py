"""Current Rakuten buyable ordinary shares joined to XNAS/XNYS listing identity."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from baibai_engine.data_collection.providers.alpaca import is_alpaca_symbol
from baibai_engine.data_collection.providers.nasdaq_symbols import (
    PrimaryUSVenue,
    USListing,
    USSymbolDirectory,
)

_BUYABLE_VENUES: dict[str, PrimaryUSVenue] = {"NASDAQ": "XNAS", "NYSE": "XNYS"}
# NYSE CQS .A/.B are regular classes; preferred and warrant suffixes are distinct.
# The primary native name is display text and can omit "Common Stock" (e.g. LEN.B).
_NYSE_BROKER_CLASS = re.compile(r"([A-Z]+) ([AB])")

type CoverageState = Literal[
    "eligible",
    "sell_only",
    "buy_suspended",
    "unknown_buyability",
    "unsupported_venue",
    "unmapped_alpaca_symbol",
    "missing_listing_directory",
    "no_matching_listing",
    "ambiguous_primary_listing",
    "exchange_fund",
]


@dataclass(frozen=True, slots=True)
class RakutenOrdinaryRow:
    """One ordinary-share screening input, not a promise of order acceptance."""

    ticker: str
    exchange: str
    buy_status: str


@dataclass(frozen=True, slots=True)
class RakutenOrdinarySnapshot:
    catalog_mode: str
    observed_at: datetime
    reported_count: int
    exclusions_reported_count: int | None
    rows: tuple[RakutenOrdinaryRow, ...]


def parse_rakuten_ordinary_snapshot(
    catalog_csv: str,
    *,
    catalog_mode: str,
    observed_at: datetime,
    reported_count: int,
    exclusions_csv: str | None = None,
    exclusions_reported_count: int | None = None,
) -> RakutenOrdinarySnapshot:
    """Read normalized current Rakuten cash data, not the vendor's raw CSV layout.

    Capture the all-market ordinary results and restrictions in one current refresh.
    The "cash_ordinary_tradable" mode requires the broker's tradable filter and a
    complete official sell-only/buy-suspension capture; the catalog-only mode is
    informational. Supply counts from the capture independently of these CSV files.
    A missing restriction capture is not a completed capture with zero rows.
    This determines screening scope only; no account or order acceptance is checked.
    """
    if catalog_mode not in ("cash_ordinary_tradable", "cash_ordinary_catalog"):
        raise ValueError("unsupported Rakuten ordinary snapshot mode")
    if observed_at.utcoffset() is None:
        raise ValueError("Rakuten snapshot time must include a UTC offset")
    if (exclusions_csv is None) != (exclusions_reported_count is None) or (
        catalog_mode == "cash_ordinary_tradable" and exclusions_csv is None
    ):
        raise ValueError("incomplete Rakuten buy restriction capture")
    catalog = _read_rakuten_snapshot_csv(catalog_csv, ("ticker", "exchange"), reported_count)
    if not catalog:
        raise ValueError("empty Rakuten ordinary catalog")
    exclusions: dict[tuple[str, str], str] = {}
    if exclusions_csv is not None and exclusions_reported_count is not None:
        for ticker, exchange, restriction in _read_rakuten_snapshot_csv(
            exclusions_csv, ("ticker", "exchange", "restriction"), exclusions_reported_count
        ):
            if restriction not in ("sell_only", "buy_suspended"):
                raise ValueError("unknown Rakuten buy restriction")
            exclusions[_rakuten_broker_key(ticker, exchange)] = restriction
    remaining = "buyable" if catalog_mode == "cash_ordinary_tradable" else "unknown"
    rows = tuple(
        RakutenOrdinaryRow(
            ticker, exchange, exclusions.get(_rakuten_broker_key(ticker, exchange), remaining)
        )
        for ticker, exchange in catalog
    )
    return RakutenOrdinarySnapshot(
        catalog_mode, observed_at, reported_count, exclusions_reported_count, rows
    )


def _read_rakuten_snapshot_csv(
    text: str, header: tuple[str, ...], reported_count: int
) -> tuple[tuple[str, ...], ...]:
    reader = csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
    try:
        if tuple(next(reader, ())) != header:
            raise ValueError("invalid Rakuten snapshot CSV header")
        rows = tuple(tuple(cell.strip() for cell in row) for row in reader)
    except csv.Error as exc:
        raise ValueError("invalid Rakuten snapshot CSV") from exc
    if type(reported_count) is not int or len(rows) != reported_count:
        raise ValueError("incomplete Rakuten snapshot CSV")
    if any(
        len(row) != len(header) or not all(cell and cell.isprintable() for cell in row)
        for row in rows
    ):
        raise ValueError("invalid Rakuten snapshot CSV row")
    if len({_rakuten_broker_key(row[0], row[1]) for row in rows}) != len(rows):
        raise ValueError("duplicate Rakuten snapshot CSV row")
    return rows


def _rakuten_join_symbol(ticker: str, venue: PrimaryUSVenue | None) -> str:
    market_class = _NYSE_BROKER_CLASS.fullmatch(ticker) if venue == "XNYS" else None
    return f"{market_class[1]}.{market_class[2]}" if market_class is not None else ticker


def _rakuten_broker_key(ticker: str, exchange: str) -> tuple[str, str]:
    # For exclusions only the same broker market and exact NYSE A/B class match;
    # Alpaca eligibility still requires the native CQS directory lookup.
    return exchange, _rakuten_join_symbol(ticker, _BUYABLE_VENUES.get(exchange))


@dataclass(frozen=True, slots=True)
class RakutenUSCoverageRow:
    ticker: str
    broker_exchange: str
    state: CoverageState
    venue: PrimaryUSVenue | None = None
    security_name: str | None = None
    alpaca_symbol: str | None = None


def cover_rakuten_us_ordinary_shares(
    broker_rows: Iterable[RakutenOrdinaryRow],
    *,
    reported_count: int,
    directories: Iterable[USSymbolDirectory],
) -> tuple[RakutenUSCoverageRow, ...]:
    """Classify a complete current broker capture, without network, storage or price admission.

    The provider's "tradable" filter alone is not a buyability proof. The current official
    catalog and restrictions establish screening scope, not order acceptance.
    NYSE class notation is mapped only with a matching native CQS listing. Eligibility here
    establishes neither availability of Alpaca observations nor execution readiness.
    """
    rows = tuple(broker_rows)
    if type(reported_count) is not int or reported_count < 1 or len(rows) != reported_count:
        raise ValueError("incomplete Rakuten ordinary-share capture")
    scope = tuple(directories)
    if not scope or len({directory.file for directory in scope}) != len(scope):
        raise ValueError("missing or repeated Nasdaq Trader directory")
    listings: dict[str, dict[PrimaryUSVenue, USListing]] = {}
    covered_venues: set[PrimaryUSVenue] = set()
    for directory in scope:
        for native_listing in directory.listings:
            covered_venues.add(native_listing.venue)
            listings.setdefault(native_listing.ticker, {})[native_listing.venue] = native_listing
    seen: set[tuple[str, str]] = set()
    results: list[RakutenUSCoverageRow] = []
    for row in rows:
        ticker, exchange = row.ticker.strip(), row.exchange.strip()
        if not ticker or not ticker.isprintable() or not exchange or not exchange.isprintable():
            raise ValueError("incomplete Rakuten ordinary-share row")
        if row.buy_status not in ("buyable", "sell_only", "buy_suspended", "unknown"):
            raise ValueError("unknown Rakuten buyability label")
        venue = _BUYABLE_VENUES.get(exchange)
        symbol = _rakuten_join_symbol(ticker, venue)
        same_symbol = listings.get(symbol, {})
        listing = same_symbol.get(venue) if venue is not None else None
        # Two verified spellings of one share class are not two broker rows.
        key = (exchange, symbol if listing is not None else ticker)
        if key in seen:
            raise ValueError("duplicate Rakuten ordinary-share row")
        seen.add(key)
        state: CoverageState
        if row.buy_status == "sell_only":
            state = "sell_only"
        elif row.buy_status == "buy_suspended":
            state = "buy_suspended"
        elif row.buy_status == "unknown":
            state = "unknown_buyability"
        elif venue is None:
            state = "unsupported_venue"
        elif venue not in covered_venues:
            state = "missing_listing_directory"
        elif not is_alpaca_symbol(symbol):
            state = "unmapped_alpaca_symbol"
        elif len(same_symbol) > 1:
            state = "ambiguous_primary_listing"
        elif listing is None:
            state = "no_matching_listing"
        elif listing.fund_flag:
            state = "exchange_fund"
        else:
            state = "eligible"
        results.append(
            RakutenUSCoverageRow(
                ticker=ticker,
                broker_exchange=exchange,
                state=state,
                venue=venue,
                alpaca_symbol=symbol if state == "eligible" else None,
                security_name=(
                    listing.security_name if listing is not None and len(same_symbol) == 1 else None
                ),
            )
        )
    return tuple(sorted(results, key=lambda row: (row.broker_exchange, row.ticker)))
