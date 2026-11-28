"""Resolve acquisition and integration target dates from their independent schedules."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

_JST = timezone(timedelta(hours=9))

# `cloud-daily-batch`'s cron in UTC (22:43 JST). A contract test pins this
# constant against the workflow so both execution and observation use one clock.
BATCH_SCHEDULED_FIRE_TIME = time(13, 43, tzinfo=UTC)
# The separate cloud-market-sources US master slot, not a JST integration trigger.
US_MASTER_SCHEDULED_FIRE_TIME = time(13, 43, tzinfo=UTC)
ECONOMIC_SCHEDULED_FIRE_TIME = time(7, 43, tzinfo=UTC)
EXTERNAL_ASSESSMENT_SCHEDULED_FIRE_TIME = time(10, 13, tzinfo=UTC)
MARKET_SOURCE_SCHEDULES = {
    "43 7 * * *": time(7, 43, tzinfo=UTC),
    "43 8 * * *": time(8, 43, tzinfo=UTC),
    "13 9 * * *": time(9, 13, tzinfo=UTC),
}


def batch_target_date(instant: datetime) -> date:
    """Return the JST day served by the latest batch cron at ``instant``."""

    fired = datetime.combine(instant.astimezone(UTC).date(), BATCH_SCHEDULED_FIRE_TIME)
    if fired > instant:
        fired -= timedelta(days=1)
    return fired.astimezone(_JST).date()


def integration_target_date(created_at: datetime, *, scheduled: bool) -> date:
    """Pin integration to the original run, including queued starts and reruns."""
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("timezone-aware integration run clock required")
    return batch_target_date(created_at) if scheduled else created_at.astimezone(_JST).date()


def acquisition_target_date(
    instant: datetime, fire_time: time = ECONOMIC_SCHEDULED_FIRE_TIME
) -> date:
    """Acquisition has its own clock, independent of the late common integration."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("timezone-aware acquisition clock required")
    fired = datetime.combine(instant.astimezone(UTC).date(), fire_time)
    if fired > instant:
        fired -= timedelta(days=1)
    return fired.astimezone(_JST).date()


def market_source_target_date(created_at: datetime, *, schedule: str, asof: str = "") -> date:
    """Resolve market acquisition from its original run clock and known cron slot."""
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("timezone-aware market source run clock required")
    fire_time = MARKET_SOURCE_SCHEDULES.get(schedule)
    if schedule and fire_time is None:
        raise ValueError("unknown market source schedule")
    if asof:
        try:
            explicit_day = date.fromisoformat(asof)
        except ValueError:
            raise ValueError("asof must use YYYY-MM-DD") from None
        if explicit_day.isoformat() != asof:
            raise ValueError("asof must use YYYY-MM-DD")
        return explicit_day
    if fire_time is not None:
        return acquisition_target_date(created_at, fire_time)
    return created_at.astimezone(_JST).date()


def us_master_refresh_date(
    created_at: datetime,
    now: datetime,
    *,
    scheduled: bool,
    is_open: Callable[[date], bool | None],
) -> date | None:
    """Plan the current NY session; None means independently verified market closure.

    The caller owns the verified US calendar (including short sessions) and input freshness.
    Scheduled runs use the last eligible slot at run creation, not at attempt start.
    GitHub can delay run creation itself; this policy never reconstructs an old run.
    Manual runs select their original NY date, never an arbitrary historical as-of.
    """
    if any(clock.utcoffset() is None for clock in (created_at, now)):
        raise ValueError("US master refresh requires timezone-aware clocks")
    created = created_at.astimezone(UTC)
    if now.astimezone(UTC) < created:
        raise ValueError("US master run creation clock is in the future")
    pinned = created
    if scheduled:
        pinned = datetime.combine(created.date(), US_MASTER_SCHEDULED_FIRE_TIME)
        if pinned > created:
            pinned -= timedelta(days=1)
    new_york = ZoneInfo("America/New_York")
    target = pinned.astimezone(new_york).date()
    if target != now.astimezone(new_york).date():
        raise ValueError("US master run is no longer for the current New York date")
    status = is_open(target)
    if status is True:
        return target
    if status is False:
        return None
    raise ValueError("US master refresh requires a verified US market calendar")


def external_assessment_target_date(created_at: datetime, now: datetime) -> date:
    """Resolve the current daily work cycle, not an exact wall-clock launch window.

    A delayed wake-up can finish yesterday's cycle after midnight. Once the next
    cycle starts, current-only pages belong to that new cycle; old missing pairs
    must not be backfilled with today's response and an invented past timestamp.
    """
    if any(clock.tzinfo is None or clock.utcoffset() is None for clock in (created_at, now)):
        raise ValueError("timezone-aware external assessment run clocks required")
    if created_at > now:
        raise ValueError("external assessment run creation clock is in the future")
    return acquisition_target_date(now, EXTERNAL_ASSESSMENT_SCHEDULED_FIRE_TIME)


def external_assessment_cycle_end(day: date) -> datetime:
    """19:13 JST starts the next current-only external-assessment cycle."""
    return datetime.combine(day + timedelta(days=1), EXTERNAL_ASSESSMENT_SCHEDULED_FIRE_TIME)


def tradingview_cycle_date(now: datetime) -> date:
    """09:00 JST starts a new acquisition cycle, not a provider observation date."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware observation clock required")
    return (now.astimezone(_JST) - timedelta(hours=9)).date()


def tradingview_observation_window(
    day: date, *, is_business_day: bool
) -> tuple[datetime, datetime]:
    """Allow delayed post-close collection until the next cycle, retaining actual UTC."""
    opening = time(15, 30) if is_business_day else time(9)
    return (
        datetime.combine(day, opening, _JST),
        datetime.combine(day + timedelta(days=1), time(9), _JST),
    )
