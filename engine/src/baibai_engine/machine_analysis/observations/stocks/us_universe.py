"""Current Rakuten buyable ordinary shares joined to XNAS/XNYS listing identity."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
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
    """One row from the ordinary-share results, with buyability established separately."""

    ticker: str
    exchange: str
    buy_status: str


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

    The provider's "tradable" filter alone is not a buyability proof. The caller establishes
    that field, verifies the captures/receipt times and obtains the complete ordinary-share scope.
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
        if row.buy_status not in ("buyable", "sell_only", "unknown"):
            raise ValueError("unknown Rakuten buyability label")
        venue = _BUYABLE_VENUES.get(exchange)
        market_class = _NYSE_BROKER_CLASS.fullmatch(ticker) if venue == "XNYS" else None
        symbol = f"{market_class[1]}.{market_class[2]}" if market_class is not None else ticker
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
