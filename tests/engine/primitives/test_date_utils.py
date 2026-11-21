from __future__ import annotations

from datetime import date

import pytest

from baibai_engine.primitives.dates import add_months_clamped


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
