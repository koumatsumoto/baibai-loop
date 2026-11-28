"""Plan Alpaca daily inputs from an explicitly selected current US listing scope."""

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
    ALPACA_ASSETS_SOURCE,
    CurrentListingMarket,
    read_bound_rakuten_scope,
)
from baibai_engine.machine_analysis.observations.stocks.us_universe import (
    cover_current_rakuten_catalog_targets,
)

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


def has_us_price_scope(current: CurrentListingMarket | None) -> bool:
    """Choose a recognized pin before calendar lookup, not price or order eligibility."""
    return current is not None and (
        current.source == ALPACA_ASSETS_SOURCE or current.rakuten is not None
    )


def plan_current_us_daily_prices(
    current: CurrentListingMarket | None,
    *,
    today_open: bool,
    price_session: date,
    cutoff: datetime,
    adjustment: Literal["raw", "split"],
    price_close_at: datetime | None = None,
    closed_session_pin: bool = False,
) -> USDailyPricePlan | None:
    """Use today's scope on an open NY day, even before its close.

    Use the explicitly supplied pin's source, not an automatic provider fallback.
    A weekend does not create a retroactive listing, broker decision or price.
    """
    if closed_session_pin and (current is None or current.source != ALPACA_ASSETS_SOURCE):
        return None
    if current is not None and current.source == ALPACA_ASSETS_SOURCE:
        return plan_current_alpaca_research_prices(
            current,
            today_open=today_open,
            price_session=price_session,
            cutoff=cutoff,
            adjustment=adjustment,
            price_close_at=price_close_at,
            closed_session_pin=closed_session_pin,
        )
    if cutoff.utcoffset() is None:
        raise ValueError("US daily price plan requires a timezone")
    today = cutoff.astimezone(_NY).date()
    if type(price_session) is not date or price_session > today:
        raise ValueError("price session cannot follow the current NY date")
    scope = read_bound_rakuten_scope(
        current, session=today if today_open else price_session, cutoff=cutoff
    )
    if scope is None or current is None:
        return None

    listed = {row.key: row for row in current.slice.rows}
    targets = {row.listing_key: row for row in scope.rows if row.state == "catalog_target"}
    coverage = cover_current_rakuten_catalog_targets(
        ((key, row.broker_symbol) for key, row in targets.items()),
        listings=listed.values(),
    )
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
        price_targets.append(USPriceTarget(key, targets[key].instrument_id))
    return _daily_plan(scope.effective_session, price_session, price_targets, adjustment)


def plan_current_alpaca_research_prices(
    current: CurrentListingMarket | None,
    *,
    today_open: bool,
    price_session: date,
    cutoff: datetime,
    adjustment: Literal["raw", "split"],
    price_close_at: datetime | None = None,
    closed_session_pin: bool = False,
) -> USDailyPricePlan | None:
    """Plan explicit current research coverage without making broker-order claims."""
    if cutoff.utcoffset() is None:
        raise ValueError("US daily price plan requires a timezone")
    today = cutoff.astimezone(_NY).date()
    if type(price_session) is not date or price_session > today:
        raise ValueError("price session cannot follow the current NY date")
    allowed_listing_sessions = (
        (price_session,)
        if closed_session_pin
        else ((today,) if today_open else (today, price_session))
    )
    if (
        current is None
        or current.source != ALPACA_ASSETS_SOURCE
        or current.received_at > cutoff
        or current.slice.session not in allowed_listing_sessions
        or price_close_at is None
        or price_close_at.utcoffset() is None
        or price_close_at > cutoff
        or price_close_at.astimezone(_NY).date() != price_session
        or (closed_session_pin and current.received_at > price_close_at)
    ):
        return None
    targets = [
        USPriceTarget(row.key, row.instrument_id)
        for row in current.slice.rows
        if row.listing_state == "listed"
        and row.instrument_id is not None
        and row.identity_observed_at is not None
        and row.identity_observed_at <= price_close_at
    ]
    return _daily_plan(current.slice.session, price_session, targets, adjustment)


def _daily_plan(
    listing_session: date,
    price_session: date,
    targets: list[USPriceTarget],
    adjustment: Literal["raw", "split"],
) -> USDailyPricePlan | None:
    if not targets:
        return None
    ordered = tuple(sorted(targets, key=lambda target: target.listing_key[1]))
    return USDailyPricePlan(
        listing_session,
        price_session,
        ordered,
        plan_alpaca_sip_daily_batches(
            (target.listing_key for target in ordered),
            session_dates=(price_session,),
            adjustment=adjustment,
        ),
    )


def _verified_window(
    readings: Iterable[AlpacaCalendarReading], *, cutoff: datetime
) -> tuple[PriceCalendar, tuple[datetime, ...], datetime, bool] | None:
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
    closes = tuple(row.close_at for row in reversed(sessions))
    return calendar, closes, max(confirmed_at, latest.close_at), known[today] is not None


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
    calendar, closes, start_clock, today_open = window
    plan = plan_current_us_daily_prices(
        current,
        today_open=today_open,
        price_session=calendar.as_of,
        price_close_at=closes[-1],
        cutoff=cutoff,
        adjustment="split",
    )
    if plan is None:
        return None
    native = {row.key: row for row in current.slice.rows}
    first: dict[str, int] = {}
    for target in plan.targets:
        row = native[target.listing_key]
        # The earliest usable bar belongs to the exact current key, not just
        # this ticker. A first observation after a short close starts later.
        if current.source == ALPACA_ASSETS_SOURCE:
            observed_at = row.identity_observed_at
            assert observed_at is not None  # The price plan already qualified the row.
            start = bisect_left(closes, observed_at)
        else:
            floor = row.identity_since
            assert floor is not None
            if row.effective_session is not None:
                floor = max(floor, row.effective_session)
            start = bisect_left(calendar.trading_dates, floor)
        first[target.listing_key[1]] = start
    broker_clocks = current.rakuten.clocks if current.rakuten is not None else ()
    return USTechnicalScope(
        plan.listing_session,
        calendar,
        plan.targets,
        first,
        max(start_clock, current.received_at, *broker_clocks),
    )
