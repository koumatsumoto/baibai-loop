"""Single horizon contract for estimate-calibration replay."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Literal

HorizonAuthority = Literal["regression_alert", "leading_evidence", "production_decision_evidence"]

# How far before an as-of a close may sit and still resolve that as-of's entry. It lives
# with the horizon contract rather than with the outcome code because both sides use it:
# the panel reports the lag it screened under, and forward resolution accepts entries
# within it. Keeping it here is also what stops a change to how outcomes are observed
# from invalidating every published panel month, which reaches this module and no other
# part of the forward build.
STALE_PRICE_MAX_LAG_DAYS = 15


@dataclass(frozen=True, slots=True)
class HorizonSpec:
    name: str
    months: int
    authority: HorizonAuthority

    def target_date(self, asof: date) -> date:
        return add_months_clamped(asof, self.months)


def add_months_clamped(value: date, months: int) -> date:
    """Add calendar months while retaining month-end semantics."""
    zero_based_month = value.month - 1 + months
    year = value.year + zero_based_month // 12
    month = zero_based_month % 12 + 1
    source_last = monthrange(value.year, value.month)[1]
    target_last = monthrange(year, month)[1]
    day = target_last if value.day == source_last else min(value.day, target_last)
    return date(year, month, day)


HORIZONS = MappingProxyType(
    {
        "3m": HorizonSpec("3m", 3, "regression_alert"),
        "6m": HorizonSpec("6m", 6, "regression_alert"),
        "1y": HorizonSpec("1y", 12, "leading_evidence"),
        "3y": HorizonSpec("3y", 36, "production_decision_evidence"),
        "5y": HorizonSpec("5y", 60, "production_decision_evidence"),
    }
)


def require_horizon(name: str) -> HorizonSpec:
    try:
        return HORIZONS[name]
    except KeyError as exc:
        raise ValueError(f"unknown calibration horizon: {name}") from exc
