"""固定価格の読取とbenchmarkモデル呼出を合成する。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from baibai_engine.machine_analysis.models.market_context.benchmark import (
    _BENCHMARK_LOOKBACK_CALENDAR_DAYS,
    NIKKEI225_ETF_PROXY,
    BenchmarkTrendSnapshot,
    benchmark_trend_from_series,
)
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars


def compute_benchmark_trend(
    sqlite_path: Path,
    asof_date: date,
    *,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
) -> BenchmarkTrendSnapshot | None:
    """Compute the benchmark trend snapshot from cached daily bars.

    Returns ``None`` when the SQLite cache is absent or holds no bars on or
    before ``asof_date``; insufficient history degrades individual fields to
    ``None`` and the state to ``unknown`` instead of failing, so callers can
    keep ranking with the diagnostic disabled while recording why.
    """
    if not sqlite_path.exists():
        return None
    benchmark_series = _load_benchmark_series(
        sqlite_path,
        asof_date,
        ticker=benchmark_ticker,
    )
    return benchmark_trend_from_series(
        benchmark_series, asof_date=asof_date, benchmark_ticker=benchmark_ticker
    )


def _load_benchmark_series(
    sqlite_path: Path,
    asof_date: date,
    *,
    ticker: str,
) -> list[tuple[date, float]]:
    bars = read_price_bars(
        sqlite_path,
        tickers=(ticker,),
        start=asof_date - timedelta(days=_BENCHMARK_LOOKBACK_CALENDAR_DAYS),
        end=asof_date,
    ).get(ticker, [])
    return [(bar.traded_at, bar.price) for bar in bars]
