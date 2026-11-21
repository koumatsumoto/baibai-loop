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


def parse_iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"invalid ISO date: {raw}") from exc
