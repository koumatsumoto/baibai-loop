from __future__ import annotations

from collections.abc import Sequence
from datetime import date


def add_business_days(start: date, n: int, calendar: Sequence[date]) -> date | None:
    """Return the nth business day strictly after ``start`` from an ascending calendar."""
    if n <= 0:
        raise ValueError("n must be positive")
    future_days = [day for day in calendar if day > start]
    if len(future_days) < n:
        return None
    return future_days[n - 1]


def weekday_distance(start: date, end: date) -> int:
    """Return the number of weekdays between two dates.

    This intentionally counts Monday-Friday only and does not subtract Japanese
    holidays. It is a bounded approximation for stale snapshot checks, not a
    full market-calendar business-day calculation.
    """
    if start == end:
        return 0
    earlier, later = (start, end) if start < end else (end, start)
    current = earlier
    count = 0
    while current < later:
        current = date.fromordinal(current.toordinal() + 1)
        if current.weekday() < 5:
            count += 1
    return count
