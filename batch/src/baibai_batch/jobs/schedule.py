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
