from __future__ import annotations

from calendar import monthrange
from datetime import date


def add_months_clamped(value: date, months: int) -> date:
    """Add calendar months while retaining month-end semantics."""
    zero_based_month = value.month - 1 + months
    year = value.year + zero_based_month // 12
    month = zero_based_month % 12 + 1
    source_last = monthrange(value.year, value.month)[1]
    target_last = monthrange(year, month)[1]
    day = target_last if value.day == source_last else min(value.day, target_last)
    return date(year, month, day)


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
