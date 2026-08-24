from __future__ import annotations

from datetime import date

from baibai_engine.foundation.date_utils import weekday_distance


def test_weekday_distance_counts_weekdays_between_dates() -> None:
    assert weekday_distance(date(2026, 5, 8), date(2026, 5, 14)) == 4


def test_weekday_distance_is_symmetric() -> None:
    assert weekday_distance(date(2026, 5, 14), date(2026, 5, 8)) == 4


def test_weekday_distance_same_day_is_zero() -> None:
    assert weekday_distance(date(2026, 5, 14), date(2026, 5, 14)) == 0
