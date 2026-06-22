from __future__ import annotations

from datetime import date, timedelta

import pytest

from baibai_loop.foundation.date_utils import add_business_days, weekday_distance


def _calendar(days: int) -> list[date]:
    start = date(2026, 4, 1)
    return [start + timedelta(days=offset) for offset in range(days)]


def test_add_business_days_15th_day_after_start() -> None:
    assert add_business_days(date(2026, 4, 1), 15, _calendar(40)) == date(2026, 4, 16)


def test_add_business_days_30th_day_after_start() -> None:
    assert add_business_days(date(2026, 4, 1), 30, _calendar(40)) == date(2026, 5, 1)


def test_add_business_days_returns_none_when_calendar_exhausted() -> None:
    assert add_business_days(date(2026, 4, 1), 30, _calendar(5)) is None


def test_add_business_days_start_at_calendar_last_day_returns_none() -> None:
    assert add_business_days(date(2026, 4, 5), 1, _calendar(5)) is None


def test_add_business_days_rejects_zero_n() -> None:
    with pytest.raises(ValueError, match="n must be positive"):
        add_business_days(date(2026, 4, 1), 0, _calendar(5))


def test_add_business_days_rejects_negative_n() -> None:
    with pytest.raises(ValueError, match="n must be positive"):
        add_business_days(date(2026, 4, 1), -1, _calendar(5))


def test_weekday_distance_counts_weekdays_between_dates() -> None:
    assert weekday_distance(date(2026, 5, 8), date(2026, 5, 14)) == 4


def test_weekday_distance_is_symmetric() -> None:
    assert weekday_distance(date(2026, 5, 14), date(2026, 5, 8)) == 4


def test_weekday_distance_same_day_is_zero() -> None:
    assert weekday_distance(date(2026, 5, 14), date(2026, 5, 14)) == 0
