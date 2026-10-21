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
    [(0, True), (1, True), (7, True)],
)
def test_delayed_prior_day_firing_uses_current_day(hour, expected):
    assert resolve_tradingview_target(
        datetime(2026, 9, 29, hour, tzinfo=UTC),
        event_name="schedule",
        schedule="37 6,8,10,12,14 * * 1-5",
    ) == (date(2026, 9, 29), "post-close-2h", expected, "")


def test_manual_uses_current_day_and_defers_market_close_check_to_preflight():
    assert resolve_tradingview_target(
        datetime(2026, 9, 28, 6, 29, tzinfo=UTC), event_name="workflow_dispatch", schedule=""
    ) == (date(2026, 9, 28), "manual", True, "")


def test_explicit_manual_observation_uses_actual_weekend_date_before_close():
    assert resolve_tradingview_target(
        datetime(2026, 10, 11, 1, tzinfo=UTC),
        event_name="workflow_dispatch",
        schedule="",
        manual_observation=True,
    ) == (date(2026, 10, 11), "manual-observation", True, "")
    with pytest.raises(ValueError, match="workflow_dispatch"):
        resolve_tradingview_target(
            datetime(2026, 10, 11, 1, tzinfo=UTC),
            event_name="schedule",
            schedule="37 6,8,10,12,14 * * 1-5",
            manual_observation=True,
        )


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


@pytest.mark.parametrize("hour", [0, 4, 8])
def test_weekend_candidate_slots_use_actual_day(hour):
    assert resolve_tradingview_target(
        datetime(2026, 10, 11, hour, 37, tzinfo=UTC),
        event_name="schedule",
        schedule="37 0,4,8 * * 0,6",
    ) == (date(2026, 10, 11), "rest-day-4h", True, "")


def test_before_nine_stops_before_hydrate():
    assert resolve_tradingview_target(
        datetime(2026, 10, 10, 23, 59, tzinfo=UTC),
        event_name="workflow_dispatch",
        schedule="",
        manual_observation=True,
    ) == (date(2026, 10, 11), "manual-observation", False, "before_observation")
