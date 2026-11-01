"""市場モデルの読取と入力固定。計算はmarket_context側へ委ねる。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.machine_analysis.models.market_context.benchmark import (
    _BENCHMARK_LOOKBACK_CALENDAR_DAYS,
    DEFAULT_MIN_BREADTH_SAMPLE,
    LONG_TREND_WINDOW_BARS,
    NIKKEI225_ETF_PROXY,
    BenchmarkTrendSnapshot,
    benchmark_trend_from_series,
)
from baibai_engine.machine_analysis.models.market_context.snapshot import (
    _POINT_LOOKBACK_CALENDAR_DAYS,
    DEFAULT_HISTORY_WEEKS,
    SECURITY_LOOKBACK_CALENDAR_DAYS,
    _Series,
    calculate_market_snapshot,
)
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.stocks.contracts import MasterSnapshot
from baibai_engine.machine_analysis.observations.stocks.reader import read_master_snapshot


@dataclass(frozen=True, slots=True)
class PreparedMarketContext:
    """単一exportの市場短期系列・master・同業候補・benchmarkを共有する。"""

    sqlite_path: Path
    asof_date: date
    history_weeks: int
    master: MasterSnapshot | None
    common_stock_tickers_by_sector: dict[str, tuple[str, ...]]
    series_by_ticker: Mapping[str, _Series]
    benchmark_ticker: str
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
            if security.is_common_stock:
                sectors.setdefault(security.sector_33, []).append(ticker)
    benchmark_rows = [
        (bar.traded_at, bar.price)
        for bar in bars_by_ticker.get(benchmark_ticker, ())
        if bar.traded_at <= asof_date
    ]
    trend_start = asof_date - timedelta(days=_BENCHMARK_LOOKBACK_CALENDAR_DAYS)
    return PreparedMarketContext(
        sqlite_path=sqlite_path.resolve(),
        asof_date=asof_date,
        history_weeks=history_weeks,
        master=master,
        common_stock_tickers_by_sector={key: tuple(value) for key, value in sectors.items()},
        series_by_ticker=series,
        benchmark_ticker=benchmark_ticker,
        benchmark_prices={day: price for day, price in benchmark_rows if day >= start},
        benchmark_trend=benchmark_trend_from_series(
            [(day, price) for day, price in benchmark_rows if day >= trend_start],
            asof_date=asof_date,
            benchmark_ticker=benchmark_ticker,
        ),
    )


def build_market_snapshot(
    *,
    sqlite_path: Path,
    asof_date: date,
    history_weeks: int = DEFAULT_HISTORY_WEEKS,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
    min_breadth_sample: int = DEFAULT_MIN_BREADTH_SAMPLE,
    prepared: PreparedMarketContext | None = None,
) -> dict[str, object]:
    """Assemble the market snapshot as of ``asof_date``.

    ``points`` walks weekly evaluation dates from oldest to ``asof_date`` so a
    reader sees how trend and breadth evolved; ``sectors``
    aggregates per-sector returns and breadth at ``asof_date`` only. Breadth
    uses common-stock membership from the latest master available at each point;
    ETFs and later master constituents cannot enter a historical stock sample.
    """
    point_dates = [
        asof_date - timedelta(days=7 * offset) for offset in range(history_weeks - 1, -1, -1)
    ]
    start = point_dates[0] - timedelta(days=_POINT_LOOKBACK_CALENDAR_DAYS)
    if prepared is not None and (
        prepared.sqlite_path != sqlite_path.resolve()
        or prepared.asof_date != asof_date
        or prepared.history_weeks != history_weeks
        or prepared.benchmark_ticker != benchmark_ticker
    ):
        raise ValueError("prepared market context does not match the requested scope")
    series_by_ticker = (
        _load_series(sqlite_path, start=start, end=asof_date)
        if prepared is None
        else {
            ticker: series.since(start)
            for ticker, series in prepared.series_by_ticker.items()
            if series.dates and series.dates[-1] >= start
        }
    )
    master = (
        read_master_snapshot(sqlite_path, as_of=asof_date) if prepared is None else prepared.master
    )
    masters_by_date = {
        day: master if day == asof_date else read_master_snapshot(sqlite_path, as_of=day)
        for day in point_dates
    }
    return calculate_market_snapshot(
        asof_date=asof_date,
        history_weeks=history_weeks,
        benchmark_ticker=benchmark_ticker,
        min_breadth_sample=min_breadth_sample,
        master=master,
        masters_by_date=masters_by_date,
        series_by_ticker=series_by_ticker,
    )


def _load_series(sqlite_path: Path, *, start: date, end: date) -> dict[str, _Series]:
    return {
        ticker: _Series(dates=[bar.traded_at for bar in bars], prices=[bar.price for bar in bars])
        for ticker, bars in read_price_bars(sqlite_path, start=start, end=end).items()
    }
