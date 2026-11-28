from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from baibai_jobs.schedule import (
    MARKET_SOURCE_SCHEDULES,
    US_MASTER_SCHEDULED_FIRE_TIME,
    us_master_refresh_date,
)


@pytest.mark.parametrize(
    ("session", "et_hour"),
    [
        (date(2026, 7, 15), 9),  # Summer: not a pre-open guarantee.
        (date(2026, 11, 23), 8),  # A JP holiday is not a US holiday.
        (date(2026, 11, 27), 8),  # A US early close still refreshes the master.
    ],
)
def test_master_uses_the_2243_jst_slot_on_a_verified_us_session(session, et_hour):
    fire = datetime.combine(session, US_MASTER_SCHEDULED_FIRE_TIME)
    assert fire.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%H:%M") == "22:43"
    assert fire.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M") == f"{et_hour:02}:43"
    assert (
        us_master_refresh_date(
            fire + timedelta(minutes=10),
            fire + timedelta(hours=8),  # The JST date has changed, but the NY date has not.
            scheduled=True,
            is_open={session: True}.get,
        )
        == session
    )


@pytest.mark.parametrize("session", [date(2026, 11, 26), date(2026, 11, 28)])
def test_verified_holiday_or_weekend_skips_but_unknown_calendar_fails(session):
    fire = datetime.combine(session, US_MASTER_SCHEDULED_FIRE_TIME)
    assert us_master_refresh_date(fire, fire, scheduled=True, is_open={session: False}.get) is None
    with pytest.raises(ValueError, match="verified US market calendar"):
        us_master_refresh_date(fire, fire, scheduled=True, is_open={}.get)


def test_manual_path_pins_the_ny_date_not_the_utc_or_jst_date():
    created = datetime.fromisoformat("2026-11-28T04:50:00Z")  # Friday 23:50 ET.
    assert us_master_refresh_date(
        created,
        created + timedelta(minutes=5),
        scheduled=False,
        is_open={date(2026, 11, 27): True}.get,
    ) == date(2026, 11, 27)


@pytest.mark.parametrize("scheduled", [True, False])
def test_prior_ny_date_never_gets_relabelled_after_midnight(scheduled):
    created = datetime.fromisoformat("2026-11-27T13:43:00Z")
    next_day = datetime.fromisoformat("2026-11-28T05:00:00Z")

    def unexpected_query(_: date) -> bool:
        pytest.fail("a superseded run must stop before reading the calendar")

    with pytest.raises(ValueError, match="no longer for the current New York date"):
        us_master_refresh_date(created, next_day, scheduled=scheduled, is_open=unexpected_query)


def test_scheduled_run_created_before_current_slot_cannot_take_that_slots_date():
    before = datetime.fromisoformat("2026-11-27T13:42:59Z")
    with pytest.raises(ValueError, match="no longer for the current New York date"):
        us_master_refresh_date(
            before, before + timedelta(minutes=3), scheduled=True, is_open={}.get
        )


def test_no_naive_or_future_run_clock():
    created = datetime(2026, 11, 27, 13, 43, tzinfo=UTC)
    for invalid in (created.replace(tzinfo=None), created - timedelta(seconds=1)):
        with pytest.raises(ValueError, match=r"timezone-aware|in the future"):
            us_master_refresh_date(created, invalid, scheduled=True, is_open={}.get)


def test_us_origin_does_not_add_a_jst_source_slot():
    assert "43 13 * * *" not in MARKET_SOURCE_SCHEDULES
