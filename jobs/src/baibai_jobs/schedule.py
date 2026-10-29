"""Resolve acquisition and integration target dates from their independent schedules."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

_JST = timezone(timedelta(hours=9))

# `cloud-daily-batch`'s cron in UTC (22:43 JST). A contract test pins this
# constant against the workflow so both execution and observation use one clock.
BATCH_SCHEDULED_FIRE_TIME = time(13, 43, tzinfo=UTC)
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
        return date.fromisoformat(asof)
    if fire_time is not None:
        return acquisition_target_date(created_at, fire_time)
    return created_at.astimezone(_JST).date()


def external_assessment_target_date(created_at: datetime, now: datetime) -> tuple[date, bool]:
    """Pin a daily run to its original GitHub creation clock, including reruns."""
    if any(clock.tzinfo is None or clock.utcoffset() is None for clock in (created_at, now)):
        raise ValueError("timezone-aware external assessment run clocks required")
    if created_at > now:
        raise ValueError("external assessment run creation clock is in the future")
    fired = datetime.combine(
        created_at.astimezone(UTC).date(), EXTERNAL_ASSESSMENT_SCHEDULED_FIRE_TIME
    )
    if fired > created_at:
        fired -= timedelta(days=1)
    return fired.astimezone(_JST).date(), now.astimezone(_JST).time() >= time(15, 30)
