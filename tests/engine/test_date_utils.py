from __future__ import annotations

from datetime import date

import pytest

from baibai_engine.foundation.date_utils import add_months_clamped, weekday_distance


@pytest.mark.parametrize(
    ("value", "months", "expected"),
    [
        (date(2024, 1, 31), 1, date(2024, 2, 29)),
        (date(2024, 2, 29), 12, date(2025, 2, 28)),
        (date(2025, 2, 28), -12, date(2024, 2, 29)),
        (date(2025, 3, 31), -6, date(2024, 9, 30)),
        (date(2025, 6, 30), -6, date(2024, 12, 31)),
        (date(2025, 6, 30), -3, date(2025, 3, 31)),
        (date(2025, 1, 15), -1, date(2024, 12, 15)),
        (date(2025, 1, 30), 1, date(2025, 2, 28)),
    ],
)
def test_add_months_clamped(value: date, months: int, expected: date) -> None:
    assert add_months_clamped(value, months) == expected


def test_weekday_distance_counts_weekdays_between_dates() -> None:
    assert weekday_distance(date(2026, 5, 8), date(2026, 5, 14)) == 4


def test_weekday_distance_is_symmetric() -> None:
    assert weekday_distance(date(2026, 5, 14), date(2026, 5, 8)) == 4


def test_weekday_distance_same_day_is_zero() -> None:
    assert weekday_distance(date(2026, 5, 14), date(2026, 5, 14)) == 0
