"""Shared semantic checks for an official period-return observation."""

from __future__ import annotations

from datetime import date, datetime
from math import isfinite
from typing import Literal


def benchmark_observation_error(
    *,
    period_start_date: date,
    period_end_date: date,
    source_as_of: date,
    published_at: date,
    retrieved_at: datetime,
    horizon: Literal["1y", "3y", "5y"],
    period_basis: Literal["official_explicit", "official_month_end_rule"],
    period_rule_source_url: str | None,
    cumulative_return_pct: float,
    annualized_return_pct: float | None,
    display_precision_bps: int,
) -> str | None:
    """Return a contract error without inferring source dates or values."""

    if period_start_date >= period_end_date:
        return "period_start_date must precede period_end_date"
    if period_end_date != source_as_of:
        return "period_end_date must equal source_as_of"
    years = int(horizon[:-1])
    if (
        period_end_date.year - period_start_date.year != years
        or period_start_date.month != period_end_date.month
        or abs(period_start_date.day - period_end_date.day) > 3
    ):
        return "benchmark period does not match horizon"
    if period_basis == "official_month_end_rule" and period_rule_source_url is None:
        return "official_month_end_rule requires period_rule_source_url"
    if period_basis == "official_explicit" and period_rule_source_url is not None:
        return "official_explicit forbids period_rule_source_url"
    if (horizon == "1y") != (annualized_return_pct is None):
        return "1y annualized_return_pct must be null; 3y/5y require a value"
    if not isfinite(cumulative_return_pct):
        return "cumulative_return_pct must be finite"
    if annualized_return_pct is not None:
        if not isfinite(annualized_return_pct):
            return "annualized_return_pct must be finite"
        derived = ((1 + cumulative_return_pct / 100) ** (1 / years) - 1) * 100
        tolerance_pct = display_precision_bps / 100 + 0.01
        if abs(derived - annualized_return_pct) > tolerance_pct:
            return "annualized_return_pct disagrees with cumulative_return_pct"
    if published_at > retrieved_at.date():
        return "published_at cannot be after retrieved_at"
    return None
