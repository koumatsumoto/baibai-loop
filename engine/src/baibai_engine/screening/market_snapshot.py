"""Produce benchmark-trend history and sector aggregates from cached bars.

The snapshot is the entry point for AI research on the market itself: a weekly
time series of the mechanical benchmark trend and universe breadth, plus a
sector map (per-sector return medians and breadth) at the evaluation date. It
also feeds the macro-context workflow with machine-collected inputs. All
fields are deterministic transforms of stored daily bars; thresholds and
window lengths are shared with :mod:`baibai_engine.screening.benchmark_trend` so the two
never disagree.
"""

from __future__ import annotations

import sqlite3
import statistics
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .benchmark_trend import (
    BREADTH_MA_WINDOW_BARS,
    DEFAULT_MIN_BREADTH_SAMPLE,
    LONG_TREND_WINDOW_BARS,
    NIKKEI225_ETF_PROXY,
    TREND_WINDOW_BARS,
    classify_benchmark_trend,
)

DEFAULT_HISTORY_WEEKS = 12

_SECTOR_RETURN_WINDOW_BARS = 20
_SECTOR_LONG_RETURN_WINDOW_BARS = 60
_POINT_LOOKBACK_CALENDAR_DAYS = 150


@dataclass(frozen=True, slots=True)
class _Series:
    dates: list[date]
    prices: list[float]

    def prefix(self, upto: date) -> Sequence[float]:
        return self.prices[: bisect_right(self.dates, upto)]

    def last_date_on(self, upto: date) -> date | None:
        index = bisect_right(self.dates, upto)
        return self.dates[index - 1] if index else None


def build_market_snapshot(
    *,
    sqlite_path: Path,
    asof_date: date,
    history_weeks: int = DEFAULT_HISTORY_WEEKS,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
    min_breadth_sample: int = DEFAULT_MIN_BREADTH_SAMPLE,
) -> dict[str, object]:
    """Assemble the market snapshot as of ``asof_date``.

    ``points`` walks weekly evaluation dates from oldest to ``asof_date`` so a
    reader sees how trend and breadth evolved; ``sectors``
    aggregates per-sector returns and breadth at ``asof_date`` only.
    """
    point_dates = [
        asof_date - timedelta(days=7 * offset) for offset in range(history_weeks - 1, -1, -1)
    ]
    start = point_dates[0] - timedelta(days=_POINT_LOOKBACK_CALENDAR_DAYS)
    series_by_ticker = _load_series(sqlite_path, start=start, end=asof_date)
    sector_by_ticker = _load_sectors(sqlite_path)
    benchmark = series_by_ticker.get(benchmark_ticker)
    points = [
        _point(
            point_date,
            benchmark=benchmark,
            series_by_ticker=series_by_ticker,
            min_breadth_sample=min_breadth_sample,
        )
        for point_date in point_dates
    ]
    return {
        "as_of": asof_date.isoformat(),
        "benchmark_ticker": benchmark_ticker,
        "history_weeks": history_weeks,
        "points": points,
        "sectors": _sector_table(
            series_by_ticker,
            sector_by_ticker=sector_by_ticker,
            asof_date=asof_date,
            benchmark_ticker=benchmark_ticker,
        ),
    }


def _point(
    point_date: date,
    *,
    benchmark: _Series | None,
    series_by_ticker: dict[str, _Series],
    min_breadth_sample: int,
) -> dict[str, object]:
    benchmark_prices = benchmark.prefix(point_date) if benchmark is not None else ()
    observation_date = benchmark.last_date_on(point_date) if benchmark is not None else None
    return_20d = _trailing_return(benchmark_prices, TREND_WINDOW_BARS)
    return_60d = _trailing_return(benchmark_prices, LONG_TREND_WINDOW_BARS)
    breadth, sample = _breadth(
        series_by_ticker,
        observation_date=observation_date,
        upto=point_date,
        min_sample=min_breadth_sample,
    )
    return {
        "date": point_date.isoformat(),
        "observation_date": observation_date.isoformat() if observation_date is not None else None,
        "benchmark_return_20d": return_20d,
        "benchmark_return_60d": return_60d,
        "breadth_pct_above_ma20": breadth,
        "breadth_sample_size": sample,
        "benchmark_trend": classify_benchmark_trend(return_20d).value,
    }


def _breadth(
    series_by_ticker: dict[str, _Series],
    *,
    observation_date: date | None,
    upto: date,
    min_sample: int,
) -> tuple[float | None, int]:
    if observation_date is None:
        return None, 0
    above = 0
    sample = 0
    for series in series_by_ticker.values():
        if series.last_date_on(upto) != observation_date:
            continue
        prices = series.prefix(upto)
        if len(prices) < BREADTH_MA_WINDOW_BARS:
            continue
        window = prices[-BREADTH_MA_WINDOW_BARS:]
        sample += 1
        if prices[-1] > sum(window) / len(window):
            above += 1
    if sample < min_sample:
        return None, sample
    return above / sample, sample


def _sector_table(
    series_by_ticker: dict[str, _Series],
    *,
    sector_by_ticker: dict[str, str],
    asof_date: date,
    benchmark_ticker: str,
) -> list[dict[str, object]]:
    grouped: dict[str, list[_Series]] = {}
    for ticker, series in series_by_ticker.items():
        if ticker == benchmark_ticker:
            continue
        sector = sector_by_ticker.get(ticker)
        if sector:
            grouped.setdefault(sector, []).append(series)
    table: list[dict[str, object]] = []
    for sector, members in grouped.items():
        returns_20d: list[float] = []
        returns_60d: list[float] = []
        above = 0
        breadth_sample = 0
        for series in members:
            prices = series.prefix(asof_date)
            short = _trailing_return(prices, _SECTOR_RETURN_WINDOW_BARS)
            if short is not None:
                returns_20d.append(short)
            longer = _trailing_return(prices, _SECTOR_LONG_RETURN_WINDOW_BARS)
            if longer is not None:
                returns_60d.append(longer)
            if len(prices) >= BREADTH_MA_WINDOW_BARS:
                window = prices[-BREADTH_MA_WINDOW_BARS:]
                breadth_sample += 1
                if prices[-1] > sum(window) / len(window):
                    above += 1
        if not returns_20d:
            continue
        table.append(
            {
                "sector_33": sector,
                "ticker_count": len(returns_20d),
                "median_return_20d": statistics.median(returns_20d),
                "median_return_60d": statistics.median(returns_60d) if returns_60d else None,
                "pct_above_ma20": (above / breadth_sample) if breadth_sample else None,
            }
        )
    table.sort(key=_sector_sort_key, reverse=True)
    return table


def _sector_sort_key(row: dict[str, object]) -> float:
    value = row.get("median_return_20d")
    return float(value) if isinstance(value, int | float) else 0.0


def _trailing_return(prices: Sequence[float], window_bars: int) -> float | None:
    if len(prices) < window_bars + 1:
        return None
    past = prices[-(window_bars + 1)]
    if past == 0:
        return None
    return prices[-1] / past - 1


def _load_series(sqlite_path: Path, *, start: date, end: date) -> dict[str, _Series]:
    if not sqlite_path.exists():
        return {}
    conn = sqlite3.connect(sqlite_path)
    try:
        rows = conn.execute(
            "SELECT ticker, traded_at, close, adjustment_factor FROM jquants_daily_bars "
            "WHERE traded_at >= ? AND traded_at <= ? ORDER BY ticker, traded_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    # cache の adjustment_close は incremental 取得で遡及の有無が混在するため使わず、
    # 不変イベントの adjustment_factor の後方累積で末尾基準の価格系列を組む。
    raw: dict[str, list[tuple[date, float | None, float | None]]] = {}
    for ticker, traded_at, close, adjustment_factor in rows:
        if not isinstance(traded_at, str):
            continue
        raw.setdefault(str(ticker), []).append(
            (
                date.fromisoformat(traded_at),
                float(close) if isinstance(close, int | float) else None,
                float(adjustment_factor) if isinstance(adjustment_factor, int | float) else None,
            )
        )
    series: dict[str, _Series] = {}
    for ticker, entries in raw.items():
        factor = 1.0
        dates: list[date] = []
        prices: list[float] = []
        for traded_at, close, adjustment_factor in reversed(entries):
            if close is not None:
                dates.append(traded_at)
                prices.append(close * factor)
            if adjustment_factor not in (None, 0.0, 1.0):
                assert adjustment_factor is not None
                factor *= adjustment_factor
        dates.reverse()
        prices.reverse()
        series[ticker] = _Series(dates=dates, prices=prices)
    return series


def _load_sectors(sqlite_path: Path) -> dict[str, str]:
    if not sqlite_path.exists():
        return {}
    conn = sqlite3.connect(sqlite_path)
    try:
        rows = conn.execute(
            "SELECT ticker, sector_33 FROM jquants_master_snapshots "
            "WHERE snapshot_date = (SELECT MAX(snapshot_date) "
            "FROM jquants_master_snapshots WHERE snapshot_date != 'unknown') "
            "ORDER BY ticker"
        ).fetchall()
    finally:
        conn.close()
    sectors: dict[str, str] = {}
    for ticker, sector in rows:
        if isinstance(sector, str) and sector:
            sectors[str(ticker)] = sector
    return sectors
