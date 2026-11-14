"""Same-session market conditions and traded-value activity, not candidate scores."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from itertools import pairwise
from math import fsum, isfinite
from statistics import median, median_high, median_low

from baibai_engine.machine_analysis.observations.prices.price_observation import PriceCalendar

ETF_PRICE_BENCHMARKS = {"1321": "nikkei225_etf_proxy", "1306": "topix_etf_proxy"}


def market_conditioned_returns(
    *,
    prices: Mapping[date, float],
    turnover: Mapping[date, float | None],
    benchmark_prices: Mapping[str, Mapping[date, float]],
    calendar: PriceCalendar,
) -> dict[str, object]:
    """Never compress a missing session into a multi-day return or fill from another ETF."""
    window = calendar.windows[60]
    return {
        "window_basis": "common_trading_calendar",
        "calendar_differences": 60,
        "window_start_date": None if window is None else window[0].isoformat(),
        "window_end_date": None if window is None else window[-1].isoformat(),
        "price_basis": "price_adjusted_close_not_total_return",
        "benchmarks": {
            ticker: {
                "benchmark_kind": kind,
                **_conditioned(prices, turnover, benchmark_prices.get(ticker, {}), window),
            }
            for ticker, kind in ETF_PRICE_BENCHMARKS.items()
        },
    }


def _conditioned(
    prices: Mapping[date, float],
    turnover: Mapping[date, float | None],
    benchmark: Mapping[date, float],
    window: tuple[date, ...] | None,
) -> dict[str, object]:
    own = [_valid(prices.get(day), positive=True) for day in window or ()]
    bench = [_valid(benchmark.get(day), positive=True) for day in window or ()]
    benchmark_returns = _returns(bench)
    benchmark_reason = (
        "insufficient_calendar"
        if window is None
        else "incomplete_benchmark_price_window"
        if None in bench
        else "nonfinite_benchmark_return"
        if benchmark_returns is None
        else None
    )
    result: dict[str, object] = {
        "status": "unavailable",
        "unavailable_reason": "insufficient_calendar",
        "own_valid_close_count": sum(value is not None for value in own)
        if window is not None
        else None,
        "benchmark_valid_close_count": sum(value is not None for value in bench)
        if window is not None
        else None,
        "up": None,
        "down": None,
        "flat_market_days": None,
        "turnover": _market_turnover(
            turnover,
            window=window,
            benchmark_returns=benchmark_returns,
            reason=benchmark_reason,
        ),
    }
    if window is None:
        return result
    if None in own or None in bench:
        return {**result, "unavailable_reason": "incomplete_price_window"}
    # Completeness is required before forming adjacent returns; no filtered-date pairing.
    own_returns = _returns(own)
    if own_returns is None or benchmark_returns is None:
        return {**result, "unavailable_reason": "nonfinite_return"}
    pairs = list(zip(own_returns, benchmark_returns, strict=True))
    return {
        **result,
        "status": "available",
        "unavailable_reason": None,
        "up": _condition([pair for pair in pairs if pair[1] > 0]),
        "down": _condition([pair for pair in pairs if pair[1] < 0]),
        "flat_market_days": sum(value == 0 for value in benchmark_returns),
    }


def _returns(prices: list[float | None]) -> list[float] | None:
    if None in prices:
        return None
    values = [b / a - 1 for a, b in pairwise(value for value in prices if value is not None)]
    return values if all(isfinite(value) for value in values) else None


def _market_turnover(
    turnover: Mapping[date, float | None],
    *,
    window: tuple[date, ...] | None,
    benchmark_returns: list[float] | None,
    reason: str | None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "basis": "traded_value_yen_not_share_volume",
        "window_basis": "return_end_dates_in_common_trading_window",
        "market_direction_status": "unavailable" if reason is not None else "available",
        "market_direction_unavailable_reason": reason,
        "up": None,
        "down": None,
        "flat_market_days": None,
    }
    if reason is not None:
        return result
    assert window is not None
    assert benchmark_returns is not None
    # Own closes do not determine market direction or validate an observed yen value.
    pairs = list(zip(window[1:], benchmark_returns, strict=True))
    return {
        **result,
        "up": _turnover_condition([turnover.get(day) for day, value in pairs if value > 0]),
        "down": _turnover_condition([turnover.get(day) for day, value in pairs if value < 0]),
        "flat_market_days": sum(value == 0 for value in benchmark_returns),
    }


def _turnover_condition(values: list[float | None]) -> dict[str, object]:
    valid = [checked for value in values if (checked := _valid(value, positive=False)) is not None]
    reason = (
        "no_condition_days"
        if not values
        else "missing_or_invalid_turnover"
        if len(valid) != len(values)
        else None
    )
    complete = reason is None
    return {
        "days": len(values),
        "valid_turnover_days": len(valid),
        "mean_yen": _mean(valid) if complete else None,
        "median_yen": _mean([median_low(valid), median_high(valid)]) if complete else None,
        "status": "available" if complete else "unavailable",
        "unavailable_reason": reason,
    }


def _condition(pairs: list[tuple[float, float]]) -> dict[str, object]:
    return {
        "days": len(pairs),
        "own_up_days": sum(own > 0 for own, _ in pairs),
        "outperform_days": sum(own > benchmark for own, benchmark in pairs),
        "own_mean_return": _mean([own for own, _ in pairs]) if pairs else None,
        "benchmark_mean_return": _mean([benchmark for _, benchmark in pairs]) if pairs else None,
        "mean_excess_return": _mean([own - benchmark for own, benchmark in pairs])
        if pairs
        else None,
    }


def turnover_activity(
    *,
    turnover: Mapping[date, float | None],
    calendar: PriceCalendar,
) -> dict[str, object]:
    """Use yen traded value as observed, never close-times-volume or inferred share volume."""
    window = calendar.windows[20]
    current_day = calendar.expected_price_date
    current = _valid(None if current_day is None else turnover.get(current_day), positive=False)
    previous = [
        _valid(turnover.get(day), positive=False) for day in (() if window is None else window[:-1])
    ]
    valid = [value for value in previous if value is not None]
    complete = window is not None and len(valid) == 20
    average = _mean(valid) if complete else None
    reason = (
        "insufficient_calendar"
        if window is None
        else "incomplete_turnover_window"
        if not complete
        else "missing_current_turnover"
        if current is None
        else "zero_prior_mean"
        if average == 0
        else None
    )
    ratio = None if reason is not None or average is None or current is None else current / average
    if ratio is not None and not isfinite(ratio):
        ratio, reason = None, "nonfinite_ratio"
    return {
        "basis": "traded_value_yen_not_share_volume",
        "window_basis": "prior_20_common_trading_sessions_excluding_current",
        "current_date": None if current_day is None else current_day.isoformat(),
        "current_yen": current,
        "prior_start_date": None if window is None else window[0].isoformat(),
        "prior_end_date": None if window is None else window[-2].isoformat(),
        "prior_valid_days": len(valid) if window is not None else None,
        "prior_mean_yen": average,
        "prior_median_yen": median(valid) if complete else None,
        "prior_min_yen": min(valid) if complete else None,
        "current_to_prior_mean_ratio": ratio,
        "status": "available" if reason is None else "unavailable",
        "unavailable_reason": reason,
    }


def _valid(value: float | None, *, positive: bool) -> float | None:
    if value is None or not isfinite(value) or (value <= 0 if positive else value < 0):
        return None
    return value


def _mean(values: list[float]) -> float:
    return fsum(value / len(values) for value in values)
