"""Plan Alpaca daily inputs from one pinned current US listing and broker scope."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from baibai_engine.machine_analysis.observations.prices.us_sip_coverage import (
    AlpacaSIPDailyBatch,
    plan_alpaca_sip_daily_batches,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master import ListingKey
from baibai_engine.machine_analysis.observations.stocks.us_universe import (
    cover_current_rakuten_catalog_targets,
)
from baibai_jobs.integration.listing_master_current import (
    CurrentListingMarket,
    read_current_rakuten_scope,
)


@dataclass(frozen=True, slots=True)
class USPriceTarget:
    listing_key: ListingKey
    instrument_id: str


@dataclass(frozen=True, slots=True)
class USDailyPricePlan:
    listing_session: date
    price_session: date
    targets: tuple[USPriceTarget, ...]
    batches: tuple[AlpacaSIPDailyBatch, ...]


def plan_current_us_daily_prices(
    current: CurrentListingMarket | None,
    *,
    listing_session: date,
    price_session: date,
    cutoff: datetime,
    adjustment: Literal["raw", "split"],
) -> USDailyPricePlan | None:
    """Plan one completed price session confirmed by the caller's US calendar.

    The broker scope is current at the selection cutoff, not a historical
    screening universe for the earlier price date. Do not attach an old price
    to a newly identified or moved security on the current native key.
    """
    if (
        type(listing_session) is not date
        or type(price_session) is not date
        or price_session > listing_session
    ):
        raise ValueError("price session cannot follow the current US listing session")
    scope = read_current_rakuten_scope(current, expected_session=listing_session, cutoff=cutoff)
    if scope is None or current is None:
        return None

    listed = {row.key: row for row in current.slice.rows}
    targets = {row.listing_key: row for row in scope.rows if row.state == "catalog_target"}
    coverage = cover_current_rakuten_catalog_targets(
        ((key, row.broker_symbol) for key, row in targets.items()),
        listings=listed.values(),
    )
    eligible = []
    price_targets: list[USPriceTarget] = []
    for row in coverage:
        if row.state != "eligible" or row.venue is None or row.alpaca_symbol is None:
            continue
        key = (row.venue, row.alpaca_symbol)
        listing = listed[key]
        if (
            listing.identity_since is None
            or listing.identity_since > price_session
            or (listing.effective_session is not None and listing.effective_session > price_session)
        ):
            continue
        eligible.append(row)
        price_targets.append(USPriceTarget(key, targets[key].instrument_id))
    if not eligible:
        return None
    batches = plan_alpaca_sip_daily_batches(
        eligible, session_dates=(price_session,), adjustment=adjustment
    )
    return USDailyPricePlan(
        listing_session,
        price_session,
        tuple(sorted(price_targets, key=lambda target: target.listing_key[1])),
        batches,
    )
