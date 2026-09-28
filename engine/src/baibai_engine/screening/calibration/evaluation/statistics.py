"""statistics for evaluation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite, sqrt
from statistics import fmean, median

from baibai_engine.market.benchmark import TOPIX_ETF_PROXY
from baibai_engine.screening.calibration.evaluation.policy import (
    DECILES,
    MIN_AXIS_SAMPLE,
    MIN_IC_SAMPLE,
    TRAP_EXCESS_THRESHOLD,
)
from baibai_engine.screening.calibration.forward import (
    ForwardReturnRow,
)
from baibai_engine.screening.calibration.horizons import require_horizon
from baibai_engine.screening.calibration.panel import PanelRow


@dataclass(frozen=True, slots=True, kw_only=True)
class _CohortExcessContext:
    population: list[PanelRow]
    excess: dict[str, float]
    population_median_return: float
    benchmark_price_return: float | None
    stale_price_count: int
    dividend_yield_coverage: int
    horizon_years: float


def _status_counts(statuses: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return counts


def _resolved_price_returns(
    forward_rows: Sequence[ForwardReturnRow], *, horizon: str
) -> tuple[dict[str, float], int]:
    price_returns: dict[str, float] = {}
    stale_count = 0
    for row in forward_rows:
        if row.horizon != horizon or not row.resolved or row.price_return is None:
            continue
        price_returns[row.ticker] = row.price_return
        if row.stale_price:
            stale_count += 1
    return price_returns, stale_count


def _context_from_returns(
    panel: Sequence[PanelRow],
    price_returns: Mapping[str, float],
    *,
    horizon: str,
    stale_count: int,
) -> _CohortExcessContext | None:
    population = [row for row in panel if row.in_population and row.ticker in price_returns]
    if len(population) < MIN_AXIS_SAMPLE:
        return None
    years = require_horizon(horizon).months / 12
    returns = {row.ticker: price_returns[row.ticker] for row in population}
    population_median_return = median(returns[row.ticker] for row in population)
    excess = {row.ticker: returns[row.ticker] - population_median_return for row in population}
    benchmark_return = price_returns.get(TOPIX_ETF_PROXY)
    return _CohortExcessContext(
        population=population,
        excess=excess,
        population_median_return=population_median_return,
        benchmark_price_return=benchmark_return,
        stale_price_count=stale_count,
        dividend_yield_coverage=0,
        horizon_years=years,
    )


def _cohort_excess_context(
    panel: Sequence[PanelRow],
    forward_rows: Sequence[ForwardReturnRow],
    *,
    horizon: str,
) -> _CohortExcessContext | None:
    price_returns, stale_count = _resolved_price_returns(forward_rows, horizon=horizon)
    return _context_from_returns(panel, price_returns, horizon=horizon, stale_count=stale_count)


def _weighted_mean(values: Sequence[tuple[float, int]]) -> float | None:
    weight = sum(item_weight for _, item_weight in values)
    if not weight:
        return None
    return round(sum(value * item_weight for value, item_weight in values) / weight, 6)


def _rounded_delta(left: object, right: object) -> float | None:
    if not isinstance(left, int | float) or not isinstance(right, int | float):
        return None
    return round(float(left) - float(right), 6)


def _annualize_return(value: float, *, years: float) -> float | None:
    if years <= 0 or value < -1:
        return None
    annualized = (1 + value) ** (1 / years) - 1
    return float(annualized) if isfinite(annualized) else None


def _group_stats(values: Sequence[float]) -> dict[str, object]:
    if not values:
        return {"n": 0, "median_excess": None, "mean_excess": None, "trap_rate": None}
    return {
        "n": len(values),
        "median_excess": round(median(values), 6),
        "mean_excess": round(fmean(values), 6),
        "trap_rate": round(
            sum(1 for value in values if value < TRAP_EXCESS_THRESHOLD) / len(values), 4
        ),
    }


def _decile_values(pairs: Sequence[tuple[float, float]]) -> list[list[float]]:
    """decile 1 (index 0) = 軸の最悪側、decile 10 (index -1) = 最良 (割安) 側の excess 群。"""
    ordered = sorted(pairs, key=lambda item: item[0])
    step = len(ordered) / DECILES
    return [
        [excess for _, excess in ordered[int(index * step) : int((index + 1) * step)]]
        for index in range(DECILES)
    ]


def _spearman(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < MIN_IC_SAMPLE:
        return None
    xs = _average_ranks([x for x, _ in pairs])
    ys = _average_ranks([y for _, y in pairs])
    mean_x = fmean(xs)
    mean_y = fmean(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (sqrt(var_x) * sqrt(var_y))


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        tie_end = index
        while tie_end + 1 < len(order) and values[order[tie_end + 1]] == values[order[index]]:
            tie_end += 1
        average_rank = (index + tie_end) / 2 + 1
        for position in range(index, tie_end + 1):
            ranks[order[position]] = average_rank
        index = tie_end + 1
    return ranks
