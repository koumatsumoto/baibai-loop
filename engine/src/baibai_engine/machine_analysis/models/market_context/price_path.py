"""Benchmark comparisons for small absolute observation pairs; no model payload reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from math import isfinite

from baibai_engine.market.price_observation import PriceComparisonPair


def compare_price_path(
    pairs: Sequence[PriceComparisonPair],
    *,
    benchmark_prices: Mapping[date, float],
    as_of: date,
    benchmark_ticker: str,
) -> dict[str, object]:
    """Missing benchmark endpoints affect only this comparison, never TA evidence."""
    comparisons: dict[str, object] = {}
    for pair in pairs:
        start = _valid_price(benchmark_prices.get(pair.start_date))
        end = _valid_price(benchmark_prices.get(pair.end_date))
        own_return = pair.end_price / pair.start_price - 1
        benchmark_return = None if start is None or end is None else end / start - 1
        comparisons[pair.observation_ref] = {
            "comparison_start_date": pair.start_date.isoformat(),
            "comparison_end_date": pair.end_date.isoformat(),
            "own_return": own_return,
            "benchmark_start_price": start,
            "benchmark_end_price": end,
            "benchmark_return": benchmark_return,
            "relative_return": None if benchmark_return is None else own_return - benchmark_return,
            "status": "available" if benchmark_return is not None else "missing_benchmark_endpoint",
        }
    return {
        "calculation_revision": "price-path-exact-endpoint-benchmark-v1",
        "as_of": as_of.isoformat(),
        "benchmark_ticker": benchmark_ticker,
        "price_basis": "price_adjusted_close",
        "adjustment_as_of": as_of.isoformat(),
        "comparisons": comparisons,
    }


def _valid_price(value: float | None) -> float | None:
    return value if value is not None and isfinite(value) and value > 0 else None
