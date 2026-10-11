from datetime import UTC, date, datetime

import pytest

from baibai_batch.jobs.schedule import resolve_tradingview_target


def test_scheduled_slot_uses_actual_jst_day():
    assert resolve_tradingview_target(
        datetime(2026, 9, 28, 12, tzinfo=UTC),
        event_name="schedule",
        schedule="37 6,8,10,12,14 * * 1-5",
    ) == (date(2026, 9, 28), "post-close-2h", True, "")


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(1, False), (7, True)],
)
def test_delayed_prior_day_firing_uses_current_day(hour, expected):
    assert resolve_tradingview_target(
        datetime(2026, 9, 29, hour, tzinfo=UTC),
        event_name="schedule",
        schedule="37 6,8,10,12,14 * * 1-5",
    ) == (date(2026, 9, 29), "post-close-2h", expected, "" if expected else "before_close")


def test_manual_uses_current_day_and_close_guard():
    assert resolve_tradingview_target(
        datetime(2026, 9, 28, 6, 29, tzinfo=UTC), event_name="workflow_dispatch", schedule=""
    ) == (date(2026, 9, 28), "manual", False, "before_close")


@pytest.mark.parametrize(
    ("now", "event", "cron"),
    [
        (datetime(2026, 9, 28, 7), "schedule", "37 6,8,10,12,14 * * 1-5"),  # noqa: DTZ001
        (datetime(2026, 9, 28, 7, tzinfo=UTC), "schedule", "unknown"),
        (datetime(2026, 9, 28, 7, tzinfo=UTC), "push", ""),
    ],
)
def test_invalid_target_fails_closed(now, event, cron):
    with pytest.raises(ValueError, match=r"timezone-aware|unknown"):
        resolve_tradingview_target(now, event_name=event, schedule=cron)
