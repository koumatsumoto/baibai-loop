"""Stop the L1 daily screening path from drifting away from its scheduled as-of."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

_JST = timezone(timedelta(hours=9))

# `cloud-daily-batch`'s cron in UTC (22:43 JST). A contract test pins this
# constant against the workflow so both execution and observation use one clock.
BATCH_SCHEDULED_FIRE_TIME = time(13, 43, tzinfo=UTC)
ECONOMIC_SCHEDULED_FIRE_TIME = time(7, 43, tzinfo=UTC)
EXTERNAL_ASSESSMENT_SCHEDULED_FIRE_TIME = time(10, 13, tzinfo=UTC)


def batch_target_date(instant: datetime) -> date:
    """Return the JST day served by the latest batch cron at ``instant``."""

    fired = datetime.combine(instant.astimezone(UTC).date(), BATCH_SCHEDULED_FIRE_TIME)
    if fired > instant:
        fired -= timedelta(days=1)
    return fired.astimezone(_JST).date()



def acquisition_target_date(instant: datetime, fire_time: time = ECONOMIC_SCHEDULED_FIRE_TIME) -> date:
    """Acquisition has its own clock, independent of the late common integration."""
    fired = datetime.combine(instant.astimezone(UTC).date(), fire_time)
    if fired > instant:
        fired -= timedelta(days=1)
    return fired.astimezone(_JST).date()

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


def resolve_tradingview_target(
    now: datetime, *, event_name: str, schedule: str, manual_observation: bool = False
) -> tuple[date, str, bool, str]:
    """Resolve the actual JST run day and its scheduled acquisition slot."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware observation clock required")
    if manual_observation and event_name != "workflow_dispatch":
        raise ValueError("manual observation requires workflow_dispatch")
    if event_name == "schedule":
        if schedule not in {"37 6,8,10,12,14 * * 1-5", "37 0,4,8 * * 0,6"}:
            raise ValueError("unknown TradingView schedule")
        slot = "rest-day-4h" if schedule == "37 0,4,8 * * 0,6" else "post-close-2h"
    elif event_name == "workflow_dispatch":
        slot = "manual-observation" if manual_observation else "manual"
    else:
        raise ValueError("unknown TradingView event")
    local = now.astimezone(_JST)
    # The hydrated JPX calendar decides business/rest-day hours in preflight.
    eligible = local.time() >= time(9)
    return local.date(), slot, eligible, "" if eligible else "before_observation"
