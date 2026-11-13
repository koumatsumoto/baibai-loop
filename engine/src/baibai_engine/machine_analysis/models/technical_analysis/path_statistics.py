"""Describe a complete close path; neither a ranking nor an execution rule."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from itertools import pairwise
from math import fsum, isfinite, log

from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar


def close_path_statistics(
    bars: Mapping[date, PriceBar], *, window: tuple[date, ...] | None
) -> dict[str, object]:
    """The caller supplies only validated priced days on one adjustment basis."""
    result: dict[str, object] = {
        "status": "insufficient_calendar" if window is None else "incomplete_price_window",
        "up_days": None,
        "down_days": None,
        "flat_days": None,
        "log_path_efficiency": None,
        "largest_absolute_log_change_share": None,
        "efficiency_unavailable_reason": None,
        "max_close_drawdown": None,
        "turnover_comparison": None,
    }
    if window is None or any(day not in bars for day in window):
        return result
    selected = [bars[day] for day in window]
    prices = [bar.price for bar in selected]
    if any(not isfinite(price) or price <= 0 for price in prices):
        return {**result, "status": "invalid_price"}
    changes = [log(current) - log(previous) for previous, current in pairwise(prices)]
    length = fsum(abs(change) for change in changes)
    up = [bar for previous, bar in pairwise(selected) if bar.price > previous.price]
    down = [bar for previous, bar in pairwise(selected) if bar.price < previous.price]
    peak = prices[0]
    drawdown = 0.0
    for price in prices:
        peak = max(peak, price)
        drawdown = min(drawdown, price / peak - 1)
    return {
        **result,
        "status": "available",
        "up_days": len(up),
        "down_days": len(down),
        "flat_days": len(changes) - len(up) - len(down),
        "log_path_efficiency": (log(prices[-1]) - log(prices[0])) / length if length else None,
        "efficiency_unavailable_reason": None if length else "constant_price",
        "largest_absolute_log_change_share": max(map(abs, changes)) / length if length else None,
        "max_close_drawdown": drawdown,
        "turnover_comparison": _turnover_comparison(up, down),
    }


def _turnover_comparison(up: list[PriceBar], down: list[PriceBar]) -> dict[str, object]:
    means = {}
    counts = {}
    missing = False
    for name, group in (("up", up), ("down", down)):
        values = [bar.turnover_value for bar in group]
        valid = [value for value in values if value is not None and isfinite(value) and value >= 0]
        complete = len(valid) == len(values)
        counts[f"{name}_valid_turnover_days"] = len(valid)
        missing |= not complete
        means[f"{name}_mean_yen"] = (
            fsum(value / len(valid) for value in valid) if valid and complete else None
        )
    numerator, denominator = means["up_mean_yen"], means["down_mean_yen"]
    reason = (
        "missing_or_invalid_turnover"
        if missing
        else "missing_up_or_down_days"
        if not up or not down
        else "zero_down_mean"
        if denominator == 0
        else None
    )
    ratio = None
    if reason is None:
        assert numerator is not None
        assert denominator is not None
        ratio = numerator / denominator
        if not isfinite(ratio):
            ratio, reason = None, "nonfinite_ratio"
    return {
        "basis": "traded_value_yen_not_share_volume",
        "status": "available" if reason is None else "unavailable",
        **counts,
        **means,
        "up_down_mean_ratio": ratio,
        "unavailable_reason": reason,
    }
