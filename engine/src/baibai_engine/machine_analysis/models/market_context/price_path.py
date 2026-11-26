"""Benchmark comparisons for small absolute observation pairs; no model payload reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Context, Decimal, localcontext
from math import isfinite

from baibai_engine.machine_analysis.observations.prices.price_observation import PriceComparisonPair


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
        if start is None or end is None:
            benchmark_return = relative_return = None
        else:
            benchmark_return = end / start - 1
            relative_return = (
                _excess(pair, start, end)
                if isfinite(own_return) and isfinite(benchmark_return)
                else own_return - benchmark_return
            )
        comparisons[pair.observation_ref] = {
            "comparison_start_date": pair.start_date.isoformat(),
            "comparison_end_date": pair.end_date.isoformat(),
            "own_return": own_return,
            "benchmark_start_price": start,
            "benchmark_end_price": end,
            "benchmark_return": benchmark_return,
            "relative_return": relative_return,
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


def _excess(pair: PriceComparisonPair, start: float, end: float) -> float:
    own_start, own_end = Decimal(str(pair.start_price)), Decimal(str(pair.end_price))
    market_start, market_end = Decimal(str(start)), Decimal(str(end))
    # A binary64 price needs at most 17 decimal digits. Two such prices can
    # be multiplied exactly here, so equivalent moves cannot acquire a sign.
    with localcontext(Context(prec=34)):
        return float((own_end * market_start - market_end * own_start) / (own_start * market_start))


def _valid_price(value: float | None) -> float | None:
    return value if value is not None and isfinite(value) and value > 0 else None
