"""Small, pure inputs shared by absolute price paths and exact-endpoint comparisons."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Literal

PriceDayState = Literal["priced", "no_trade", "mixed", "invalid"]
PRICE_PATH_DIFFERENCES = (1, 20, 60, 252)


@dataclass(frozen=True, slots=True)
class PriceComparisonPair:
    observation_ref: str
    start_date: date
    end_date: date
    start_price: float
    end_price: float


@dataclass(frozen=True, slots=True)
class PriceCalendar:
    as_of: date
    expected_price_date: date | None
    windows: Mapping[int, tuple[date, ...] | None]


def price_calendar(calendar: Mapping[date, bool], *, as_of: date) -> PriceCalendar:
    """Resolve each window independently; an unknown civil day cannot shorten a window."""
    dates = tuple(sorted(day for day, business in calendar.items() if business and day <= as_of))
    expected = dates[-1] if dates and as_of in calendar else None
    if expected is not None and not _continuous(calendar, expected, as_of):
        expected = None
    windows: dict[int, tuple[date, ...] | None] = {}
    for differences in PRICE_PATH_DIFFERENCES:
        window = dates[-(differences + 1) :]
        windows[differences] = (
            window
            if expected is not None
            and len(window) == differences + 1
            and _continuous(calendar, window[0], as_of)
            else None
        )
    return PriceCalendar(as_of, expected, windows)


def classify_price_day(values: Sequence[float | None]) -> PriceDayState:
    """Classify saved O/H/L/C/Vo/Va, not the reason for a day without executions.

    J-Quants returns all six null for no executions, including a full-day suspension.
    This uses the canonical projection of those six native fields. A partial null row
    cannot establish that meaning; a close alone is not a validated priced row here.
    """
    if len(values) != 6:
        raise ValueError("price day classification requires O/H/L/C/Vo/Va")
    if all(value is None for value in values):
        return "no_trade"
    if any(value is None for value in values):
        return "mixed"
    present = [value for value in values if value is not None]
    if (
        any(not isfinite(value) for value in present)
        or any(value <= 0 for value in present[:4])
        or any(value < 0 for value in present[4:])
    ):
        return "invalid"
    return "priced"


def _continuous(calendar: Mapping[date, bool], start: date, end: date) -> bool:
    return sum(start <= day <= end for day in calendar) == (end - start).days + 1
