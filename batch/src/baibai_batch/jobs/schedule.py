"""Stop the L1 daily screening path from drifting away from its scheduled as-of."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

_JST = timezone(timedelta(hours=9))

# `cloud-daily-batch`'s cron in UTC (16:43 JST). A contract test pins this
# constant against the workflow so both execution and observation use one clock.
BATCH_SCHEDULED_FIRE_TIME = time(7, 43, tzinfo=UTC)


def batch_target_date(instant: datetime) -> date:
    """Return the JST day served by the latest batch cron at ``instant``."""

    fired = datetime.combine(instant.astimezone(UTC).date(), BATCH_SCHEDULED_FIRE_TIME)
    if fired > instant:
        fired -= timedelta(days=1)
    return fired.astimezone(_JST).date()


def resolve_tradingview_target(
    now: datetime, *, event_name: str, schedule: str
) -> tuple[date, str, bool, str]:
    """Resolve the actual JST run day and its scheduled acquisition slot."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware observation clock required")
    slots = {
        "57 6 * * 1-5": "slot-1557",
        "7 9 * * 1-5": "slot-1807",
        "17 11 * * 1-5": "slot-2017",
    }
    if event_name == "schedule":
        if schedule not in slots:
            raise ValueError("unknown TradingView schedule")
        slot = slots[schedule]
    elif event_name == "workflow_dispatch":
        slot = "manual"
    else:
        raise ValueError("unknown TradingView event")
    local = now.astimezone(_JST)
    eligible = local.time() >= time(15, 30)
    return local.date(), slot, eligible, "" if eligible else "before_close"
