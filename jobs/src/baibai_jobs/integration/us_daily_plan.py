"""Plan Alpaca daily inputs from one pinned current US listing and broker scope."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.alpaca_calendar import (
    CALENDAR_WINDOW_MAX_DAYS,
    AlpacaCalendarReading,
    USRegularSession,
)
from baibai_engine.machine_analysis.observations.prices.price_observation import (
    PriceCalendar,
    price_calendar,
)
from baibai_engine.machine_analysis.observations.prices.us_sip_coverage import (
    AlpacaSIPDailyBatch,
    plan_alpaca_sip_daily_batches,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master import ListingKey
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    CurrentListingMarket,
    CurrentRakutenScope,
    read_bound_rakuten_scope,
)
from baibai_engine.machine_analysis.observations.stocks.us_universe import (
    cover_current_rakuten_catalog_targets,
)
from baibai_jobs.integration.listing_master_current import read_current_rakuten_scope

TECHNICAL_WINDOW_SESSIONS = 253
TECHNICAL_CALENDAR_CHUNKS = 16
_NY = ZoneInfo("America/New_York")
_ONE_DAY = timedelta(days=1)


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
    return _plan_prices(current, scope, price_session=price_session, adjustment=adjustment)


def plan_confirmed_closed_us_daily_prices(
    current: CurrentListingMarket | None,
    *,
    price_session: date,
    cutoff: datetime,
    adjustment: Literal["raw", "split"],
) -> USDailyPricePlan | None:
    """Pick the prior session's exact broker scope, when today's NY closure is proved."""
    scope = read_bound_rakuten_scope(current, session=price_session, cutoff=cutoff)
    return _plan_prices(current, scope, price_session=price_session, adjustment=adjustment)


def _plan_prices(
    current: CurrentListingMarket | None,
    scope: CurrentRakutenScope | None,
    *,
    price_session: date,
    adjustment: Literal["raw", "split"],
) -> USDailyPricePlan | None:
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
        scope.effective_session,
        price_session,
        tuple(sorted(price_targets, key=lambda target: target.listing_key[1])),
        batches,
    )


def _verified_window(
    readings: Iterable[AlpacaCalendarReading], *, cutoff: datetime
) -> tuple[PriceCalendar, datetime, bool] | None:
    today = cutoff.astimezone(_NY).date()
    known: dict[date, USRegularSession | None] = {}
    confirmed_at: datetime | None = None
    for reading in readings:
        stamp = reading.received_at
        length = (reading.end - reading.start).days + 1
        if (
            reading.problem is not None
            or stamp is None
            or stamp.utcoffset() is None
            or stamp > cutoff
            or not 1 <= length <= CALENDAR_WINDOW_MAX_DAYS
        ):
            return None
        confirmed_at = stamp if confirmed_at is None else max(confirmed_at, stamp)
        active = {row.trading_date: row for row in reading.sessions}
        for index in range(length):
            day = reading.start + index * _ONE_DAY
            row = active.get(day)
            if day in known and known[day] != row:
                return None
            known[day] = row
    if confirmed_at is None or today not in known:
        return None
    # Walk civil days, not just provider's open dates, so missing calendar pages
    # can never silently shorten the momentum/path windows.
    sessions: list[USRegularSession] = []
    day = today
    while len(sessions) < TECHNICAL_WINDOW_SESSIONS:
        if day not in known:
            return None
        row = known[day]
        if row is not None and row.close_at <= cutoff:
            sessions.append(row)
        day -= _ONE_DAY
    start, latest = sessions[-1].trading_date, sessions[0]
    calendar = price_calendar(
        {day: row is not None for day, row in known.items() if start <= day <= latest.trading_date},
        as_of=latest.trading_date,
    )
    return calendar, max(confirmed_at, latest.close_at), known[today] is None


@dataclass(frozen=True, slots=True)
class USTechnicalScope:
    listing_session: date
    calendar: PriceCalendar
    targets: tuple[USPriceTarget, ...]
    first: Mapping[str, int]
    not_before: datetime


def plan_us_technical_scope(
    current: CurrentListingMarket | None,
    calendars: Iterable[AlpacaCalendarReading],
    *,
    cutoff: datetime,
) -> USTechnicalScope | None:
    """Share the same qualified current universe and continuity floor across both ends."""
    if cutoff.utcoffset() is None:
        raise ValueError("US technical cutoff requires a timezone")
    window = _verified_window(calendars, cutoff=cutoff)
    if current is None or window is None:
        return None
    calendar, start_clock, closed_today = window
    days = calendar.trading_dates
    session = calendar.as_of
    plan = (
        plan_confirmed_closed_us_daily_prices(
            current, price_session=session, cutoff=cutoff, adjustment="split"
        )
        if closed_today
        else plan_current_us_daily_prices(
            current,
            listing_session=cutoff.astimezone(_NY).date(),
            price_session=session,
            cutoff=cutoff,
            adjustment="split",
        )
    )
    if plan is None or current.rakuten is None:
        return None
    native = {row.key: row for row in current.slice.rows}
    first: dict[str, int] = {}
    for target in plan.targets:
        row = native[target.listing_key]
        # This is only the proven floor for the current exact key, not an IPO
        # date. In particular the current token cannot cross a known move/reuse.
        floor = row.identity_since
        assert floor is not None  # The existing price plan requires it.
        if row.effective_session is not None:
            floor = max(floor, row.effective_session)
        first[target.listing_key[1]] = bisect_left(days, floor)
    return USTechnicalScope(
        plan.listing_session,
        calendar,
        plan.targets,
        first,
        max(start_clock, current.received_at, *current.rakuten.clocks),
    )
