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
