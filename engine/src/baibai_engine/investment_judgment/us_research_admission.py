"""Current US Research eligibility from one listing pin and a verified NY calendar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from baibai_engine.data_collection.providers.alpaca_calendar import (
    AlpacaCalendarReading,
    USRegularSession,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master import ListingKey
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    RAKUTEN_SCOPE_ZONE,
    CurrentListingMarket,
    read_bound_rakuten_scope,
)
from baibai_engine.machine_analysis.observations.stocks.us_universe import (
    cover_current_rakuten_catalog_targets,
)


@dataclass(frozen=True, slots=True)
class USResearchListingBasis:
    """An exact instrument-instance binding for new analysis, not order readiness."""

    listing_session_et: date
    instrument_id: str

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("US Research listing identity is empty")


def _sessions_at(
    calendar: AlpacaCalendarReading, instant: datetime
) -> tuple[date, USRegularSession]:
    day = instant.astimezone(RAKUTEN_SCOPE_ZONE).date()
    if not calendar.start <= day <= calendar.end:
        raise ValueError("US Research requires verified cash calendar through its cutoff")
    dates = [item for item in calendar.sessions if item.trading_date <= day]
    completed = [item for item in dates if item.close_at.astimezone(UTC) <= instant]
    if not completed:
        raise ValueError("US Research has no verified completed US price session")
    return (
        max(item.trading_date for item in dates),
        max(completed, key=lambda item: item.trading_date),
    )


def require_us_research_listing(
    current: CurrentListingMarket | None,
    calendar: AlpacaCalendarReading,
    *,
    key: ListingKey,
    cutoff: datetime,
    checked_at: datetime,
    price_session: date,
    session_close_at: datetime,
) -> USResearchListingBasis:
    """Do not bind old prices to a relisted/reused/venue-transferred security."""
    if any(instant.utcoffset() is None for instant in (cutoff, checked_at, session_close_at)):
        raise ValueError("US Research admission requires timezone-aware clocks")
    cutoff, checked_at = cutoff.astimezone(UTC), checked_at.astimezone(UTC)
    received = calendar.received_at
    if (
        cutoff > checked_at
        or calendar.problem is not None
        or received is None
        or received.utcoffset() is None
        or received > cutoff
    ):
        raise ValueError("US Research requires a verified calendar received by the cutoff")
    listing_session, completed = _sessions_at(calendar, cutoff)
    if (
        (listing_session, completed) != _sessions_at(calendar, checked_at)
        or completed.trading_date != price_session
        or completed.close_at.astimezone(UTC) != session_close_at.astimezone(UTC)
    ):
        raise ValueError("US Research price session is not the latest verified completed session")
    scope = read_bound_rakuten_scope(current, session=listing_session, cutoff=cutoff)
    if scope is None or current is None:
        raise ValueError("US Research requires the current verified Rakuten ordinary scope")
    target = next(
        (row for row in scope.rows if row.listing_key == key and row.state == "catalog_target"),
        None,
    )
    listing = next((row for row in current.slice.rows if row.key == key), None)
    if target is None or listing is None:
        raise ValueError("US Research requires the exact eligible current US listing")
    (covered,) = cover_current_rakuten_catalog_targets(
        ((key, target.broker_symbol),), listings=current.slice.rows
    )
    if (
        covered.state != "eligible"
        or (covered.venue, covered.alpaca_symbol) != key
        or listing.identity_since is None
        or listing.identity_since > price_session
        or (listing.effective_session is not None and listing.effective_session > price_session)
    ):
        raise ValueError("US Research price is not bound to this eligible listing instance")
    return USResearchListingBasis(listing_session, target.instrument_id)
