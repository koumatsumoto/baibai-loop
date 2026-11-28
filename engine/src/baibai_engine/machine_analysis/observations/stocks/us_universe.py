"""Current Rakuten buyable ordinary shares joined to XNAS/XNYS listing identity."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.alpaca import is_alpaca_symbol
from baibai_engine.data_collection.providers.nasdaq_symbols import (
    PrimaryUSVenue,
    USListing,
    USSymbolDirectory,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ListingKey,
    ListingMasterRow,
)

_NY = ZoneInfo("America/New_York")
_BUYABLE_VENUES: dict[str, PrimaryUSVenue] = {"NASDAQ": "XNAS", "NYSE": "XNYS"}
RAKUTEN_LISTING_EXCHANGES = frozenset(_BUYABLE_VENUES)
_BROKER_EXCHANGE_BY_MIC: dict[str, str] = {venue: name for name, venue in _BUYABLE_VENUES.items()}
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


type RakutenRestriction = Literal["sell_only", "buy_suspended"]


@dataclass(frozen=True, slots=True)
class RakutenRestrictionRow:
    """The broker's negative observation, not a confirmed listing or security kind."""

    ticker: str
    exchange: str
    restriction: RakutenRestriction


@dataclass(frozen=True, slots=True)
class RakutenOrdinarySnapshot:
    catalog_mode: str
    effective_session: date
    catalog_received_at: datetime
    exclusions_received_at: datetime | None
    reported_count: int
    exclusions_reported_count: int | None
    rows: tuple[RakutenOrdinaryRow, ...]
    restriction_only: tuple[RakutenRestrictionRow, ...]


def _received_on_session(stamp: object, effective_session: date) -> datetime:
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError("Rakuten capture receipt time must include a UTC offset")
    if stamp.astimezone(_NY).date() != effective_session:
        raise ValueError("Rakuten capture receipt is outside its target NY session date")
    return stamp.astimezone(UTC)


def parse_rakuten_ordinary_snapshot(
    catalog_csv: str,
    *,
    catalog_mode: str,
    effective_session: date,
    catalog_received_at: datetime,
    reported_count: int,
    exclusions_csv: str | None = None,
    exclusions_received_at: datetime | None = None,
    exclusions_reported_count: int | None = None,
) -> RakutenOrdinarySnapshot:
    """Read normalized current Rakuten cash data, not the vendor's raw CSV layout.

    The acquisition caller supplies a confirmed target NY trading session and separately
    verified source scope, counts, and actual receipt times. The clocks must be on that
    date, but are not release or restriction-effective timestamps. A matching date does
    not make a partial view an authoritative catalog or permit backdating.

    Only a complete tradable ordinary catalog plus a complete restriction capture
    establishes positive screening scope. Keep restrictions outside the catalog without
    counting them as ordinary securities, inferring listing state, or requesting prices.
    """
    if catalog_mode not in ("cash_ordinary_tradable", "cash_ordinary_catalog"):
        raise ValueError("unsupported Rakuten ordinary snapshot mode")
    if type(effective_session) is not date:
        raise ValueError("Rakuten snapshot requires a target NY session date")
    if (
        (exclusions_csv is None) != (exclusions_reported_count is None)
        or (exclusions_csv is None) != (exclusions_received_at is None)
        or (catalog_mode == "cash_ordinary_tradable" and exclusions_csv is None)
    ):
        raise ValueError("incomplete Rakuten buy restriction capture")
    catalog_received_at = _received_on_session(catalog_received_at, effective_session)
    catalog = _read_rakuten_snapshot_csv(catalog_csv, ("ticker", "exchange"), reported_count)
    if not catalog:
        raise ValueError("empty Rakuten ordinary catalog")
    exclusions: dict[tuple[str, str], RakutenRestrictionRow] = {}
    if (
        exclusions_csv is not None
        and exclusions_reported_count is not None
        and exclusions_received_at is not None
    ):
        exclusions_received_at = _received_on_session(exclusions_received_at, effective_session)
        for ticker, exchange, restriction in _read_rakuten_snapshot_csv(
            exclusions_csv, ("ticker", "exchange", "restriction"), exclusions_reported_count
        ):
            if restriction not in ("sell_only", "buy_suspended"):
                raise ValueError("unknown Rakuten buy restriction")
            exclusions[_rakuten_broker_key(ticker, exchange)] = RakutenRestrictionRow(
                ticker, exchange, cast("RakutenRestriction", restriction)
            )
    remaining = "buyable" if catalog_mode == "cash_ordinary_tradable" else "unknown"
    catalog_keys: set[tuple[str, str]] = set()
    rows: list[RakutenOrdinaryRow] = []
    for ticker, exchange in catalog:
        key = _rakuten_broker_key(ticker, exchange)
        catalog_keys.add(key)
        exclusion = exclusions.get(key)
        rows.append(
            RakutenOrdinaryRow(ticker, exchange, exclusion.restriction if exclusion else remaining)
        )
    restriction_only = tuple(exclusions[key] for key in sorted(exclusions.keys() - catalog_keys))
    return RakutenOrdinarySnapshot(
        catalog_mode,
        effective_session,
        catalog_received_at,
        exclusions_received_at,
        reported_count,
        exclusions_reported_count,
        tuple(rows),
        restriction_only,
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


def rakuten_listing_key(ticker: str, exchange: str) -> tuple[PrimaryUSVenue, str] | None:
    """Return the exact native listing lookup key, not proof of listing or eligibility."""
    venue = _BUYABLE_VENUES.get(exchange)
    if venue is None:
        return None
    market_class = _NYSE_BROKER_CLASS.fullmatch(ticker) if venue == "XNYS" else None
    symbol = f"{market_class[1]}.{market_class[2]}" if market_class is not None else ticker
    return venue, symbol


def _rakuten_broker_key(ticker: str, exchange: str) -> tuple[str, str]:
    # Keep broker-only venues as native facts, never as an exchange listing key.
    listing_key = rakuten_listing_key(ticker, exchange)
    return exchange, listing_key[1] if listing_key is not None else ticker


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
    return _cover_rakuten_listings(rows, (row for directory in scope for row in directory.listings))


def cover_current_rakuten_catalog_targets(
    targets: Iterable[tuple[ListingKey, str]],
    *,
    listings: Iterable[ListingMasterRow],
) -> tuple[RakutenUSCoverageRow, ...]:
    """Use the shared symbol/issue rules with already-bound current broker targets."""
    rows: list[RakutenOrdinaryRow] = []
    for key, ticker in targets:
        exchange = _BROKER_EXCHANGE_BY_MIC.get(key[0])
        if exchange is None or rakuten_listing_key(ticker, exchange) != key:
            raise ValueError("current Rakuten target must identify the exact exchange listing")
        rows.append(RakutenOrdinaryRow(ticker, exchange, "buyable"))
    supported_listings = (
        USListing(
            cast("PrimaryUSVenue", row.key[0]),
            row.key[1],
            row.security_name,
            row.issue_type == "fund",
        )
        for row in listings
        if row.listing_state == "listed"
        and row.key[0] in _BROKER_EXCHANGE_BY_MIC
        and row.issue_type in ("fund", "not_flagged_fund")
        and row.security_name is not None
    )
    return _cover_rakuten_listings(rows, supported_listings)


def _cover_rakuten_listings(
    rows: Iterable[RakutenOrdinaryRow], native_listings: Iterable[USListing]
) -> tuple[RakutenUSCoverageRow, ...]:
    listings: dict[str, dict[PrimaryUSVenue, USListing]] = {}
    covered_venues: set[PrimaryUSVenue] = set()
    for listing_row in native_listings:
        covered_venues.add(listing_row.venue)
        listings.setdefault(listing_row.ticker, {})[listing_row.venue] = listing_row
    seen: set[tuple[str, str]] = set()
    results: list[RakutenUSCoverageRow] = []
    for row in rows:
        ticker, exchange = row.ticker.strip(), row.exchange.strip()
        if not ticker or not ticker.isprintable() or not exchange or not exchange.isprintable():
            raise ValueError("incomplete Rakuten ordinary-share row")
        if row.buy_status not in ("buyable", "sell_only", "buy_suspended", "unknown"):
            raise ValueError("unknown Rakuten buyability label")
        listing_key = rakuten_listing_key(ticker, exchange)
        venue, symbol = listing_key if listing_key is not None else (None, ticker)
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
