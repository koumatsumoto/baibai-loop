from __future__ import annotations

from datetime import date


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
