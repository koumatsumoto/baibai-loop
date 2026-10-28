"""Produce benchmark-trend history and sector aggregates from cached bars.

The snapshot is the entry point for AI research on the market itself: a weekly
time series of the mechanical benchmark trend and universe breadth, plus a
sector map (per-sector return medians and breadth) at the evaluation date. It
also feeds the explicit market context assessment with machine-collected inputs. All
fields are deterministic transforms of stored market observations. Return windows
and trend thresholds are shared with :mod:`baibai_engine.machine_analysis.models.market_context.benchmark`;
results also depend on the available history in each requested input window.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.machine_analysis.observations.stocks.contracts import (
    MasterSnapshot,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar

from baibai_engine.machine_analysis.models.market_context.benchmark import (
    _BENCHMARK_LOOKBACK_CALENDAR_DAYS,
    BREADTH_MA_WINDOW_BARS,
    LONG_TREND_WINDOW_BARS,
    NIKKEI225_ETF_PROXY,
    TREND_WINDOW_BARS,
    BenchmarkTrendSnapshot,
    benchmark_trend_from_series,
    classify_benchmark_trend,
)
from baibai_engine.machine_analysis.models.market_context.history import trailing_return

DEFAULT_HISTORY_WEEKS = 12
SECURITY_LOOKBACK_CALENDAR_DAYS = 1130

_SECTOR_RETURN_WINDOW_BARS = 20
_SECTOR_LONG_RETURN_WINDOW_BARS = 60
_POINT_LOOKBACK_CALENDAR_DAYS = 150


@dataclass(frozen=True, slots=True)
class _Series:
    dates: list[date]
    prices: list[float]

    def since(self, start: date) -> _Series:
        index = bisect_left(self.dates, start)
        return self if index == 0 else _Series(self.dates[index:], self.prices[index:])

    def prefix(self, upto: date) -> Sequence[float]:
        return self.prices[: bisect_right(self.dates, upto)]

    def last_date_on(self, upto: date) -> date | None:
        index = bisect_right(self.dates, upto)
        return self.dates[index - 1] if index else None

    def price_on(self, day: date) -> float | None:
        index = bisect_left(self.dates, day)
        return self.prices[index] if index < len(self.dates) and self.dates[index] == day else None


@dataclass(frozen=True, slots=True)
class PreparedMarketContext:
    """単一exportの市場短期系列・master・同業候補・benchmarkを共有する。"""

    sqlite_path: Path
    asof_date: date
    history_weeks: int
    start: date
    master: MasterSnapshot | None
    common_stock_tickers_by_sector: dict[str, tuple[str, ...]]
    series_by_ticker: Mapping[str, _Series]
    benchmark_ticker: str
    benchmark_start: date
    benchmark_prices: dict[date, float]
    benchmark_trend: BenchmarkTrendSnapshot | None




def prepare_market_context_from_bars(
    *,
    sqlite_path: Path,
    asof_date: date,
    master: MasterSnapshot | None,
    bars_by_ticker: Mapping[str, Sequence[PriceBar]],
    subjects: Sequence[str],
    history_weeks: int = DEFAULT_HISTORY_WEEKS,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
) -> PreparedMarketContext:
    """生成の共通長期価格を再利用する。疎な個社の比較端点も同じ束から読む。"""
    start = asof_date - timedelta(days=7 * (history_weeks - 1) + _POINT_LOOKBACK_CALENDAR_DAYS)
    lookback_start = asof_date - timedelta(days=SECURITY_LOOKBACK_CALENDAR_DAYS)
    for ticker in subjects:
        rows = [
            bar
            for bar in bars_by_ticker.get(ticker, ())
            if lookback_start <= bar.traded_at <= asof_date
        ]
        if rows:
            start = min(start, rows[-min(len(rows), LONG_TREND_WINDOW_BARS + 1)].traded_at)
    series = {}
    for ticker, bars in bars_by_ticker.items():
        selected = [bar for bar in bars if start <= bar.traded_at <= asof_date]
        if selected:
            series[ticker] = _Series(
                dates=[bar.traded_at for bar in selected], prices=[bar.price for bar in selected]
            )
    sectors: dict[str, list[str]] = {}
    if master is not None:
        for ticker, security in master.securities.items():
            sector = security["sector_33"]
            if security["is_common_stock"] and isinstance(sector, str):
                sectors.setdefault(sector, []).append(ticker)
    benchmark_rows = [
        (bar.traded_at, bar.price)
        for bar in bars_by_ticker.get(benchmark_ticker, ())
        if bar.traded_at <= asof_date
    ]
    trend_start = asof_date - timedelta(days=_BENCHMARK_LOOKBACK_CALENDAR_DAYS)
    return PreparedMarketContext(
        sqlite_path=sqlite_path,
        asof_date=asof_date,
        history_weeks=history_weeks,
        start=start,
        master=master,
        common_stock_tickers_by_sector={key: tuple(value) for key, value in sectors.items()},
        series_by_ticker=series,
        benchmark_ticker=benchmark_ticker,
        benchmark_start=start,
        benchmark_prices={day: price for day, price in benchmark_rows if day >= start},
        benchmark_trend=benchmark_trend_from_series(
            [(day, price) for day, price in benchmark_rows if day >= trend_start],
            asof_date=asof_date,
            benchmark_ticker=benchmark_ticker,
        ),
    )




def _point(
    point_date: date,
    *,
    benchmark: _Series | None,
    series_by_ticker: Mapping[str, _Series],
    breadth_tickers: set[str],
    min_breadth_sample: int,
) -> dict[str, object]:
    benchmark_prices = benchmark.prefix(point_date) if benchmark is not None else ()
    observation_date = benchmark.last_date_on(point_date) if benchmark is not None else None
    return_20d = trailing_return(benchmark_prices, TREND_WINDOW_BARS)
    return_60d = trailing_return(benchmark_prices, LONG_TREND_WINDOW_BARS)
    breadth, sample = _breadth(
        series_by_ticker,
        observation_date=observation_date,
        upto=point_date,
        min_sample=min_breadth_sample,
        tickers=breadth_tickers,
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
    series_by_ticker: Mapping[str, _Series],
    *,
    observation_date: date | None,
    upto: date,
    min_sample: int,
    tickers: set[str],
) -> tuple[float | None, int]:
    if observation_date is None:
        return None, 0
    above = 0
    sample = 0
    for ticker, series in series_by_ticker.items():
        if ticker not in tickers or series.last_date_on(upto) != observation_date:
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
    series_by_ticker: Mapping[str, _Series],
    *,
    sector_by_ticker: dict[str, str],
    asof_date: date,
    benchmark_ticker: str,
) -> list[dict[str, object]]:
    benchmark = series_by_ticker.get(benchmark_ticker)
    observation_date = benchmark.last_date_on(asof_date) if benchmark is not None else None
    if observation_date is None:
        observation_date = max(
            (
                day
                for series in series_by_ticker.values()
                if (day := series.last_date_on(asof_date)) is not None
            ),
            default=None,
        )
    if observation_date is None:
        return []
    grouped: dict[str, list[_Series]] = {}
    for ticker, series in series_by_ticker.items():
        if ticker == benchmark_ticker or series.last_date_on(asof_date) != observation_date:
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
            short = trailing_return(prices, _SECTOR_RETURN_WINDOW_BARS)
            if short is not None:
                returns_20d.append(short)
            longer = trailing_return(prices, _SECTOR_LONG_RETURN_WINDOW_BARS)
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




def calculate_market_snapshot(
    *,
    asof_date: date,
    history_weeks: int,
    benchmark_ticker: str,
    min_breadth_sample: int,
    master: MasterSnapshot | None,
    masters_by_date: Mapping[date, MasterSnapshot | None],
    series_by_ticker: Mapping[str, _Series],
) -> dict[str, object]:
    """各評価日の固定masterと価格だけからbreadth・業種比較を計算する。"""
    point_dates = [
        asof_date - timedelta(days=7 * offset) for offset in range(history_weeks - 1, -1, -1)
    ]
    sector_by_ticker = (
        {}
        if master is None
        else {
            ticker: str(row["sector_33"])
            for ticker, row in master.securities.items()
            if row["is_common_stock"]
        }
    )
    benchmark = series_by_ticker.get(benchmark_ticker)
    points = []
    for point_date in point_dates:
        point_master = masters_by_date.get(point_date)
        breadth_tickers = (
            set()
            if point_master is None
            else {
                ticker for ticker, row in point_master.securities.items() if row["is_common_stock"]
            }
        )
        points.append(
            _point(
                point_date,
                benchmark=benchmark,
                series_by_ticker=series_by_ticker,
                breadth_tickers=breadth_tickers,
                min_breadth_sample=min_breadth_sample,
            )
        )
    return {
        "as_of": asof_date.isoformat(),
        "benchmark_ticker": benchmark_ticker,
        "history_weeks": history_weeks,
        "master_snapshot_date": None if master is None else master.as_of.isoformat(),
        "points": points,
        "sectors": _sector_table(
            series_by_ticker,
            sector_by_ticker=sector_by_ticker,
            asof_date=asof_date,
            benchmark_ticker=benchmark_ticker,
        ),
    }
