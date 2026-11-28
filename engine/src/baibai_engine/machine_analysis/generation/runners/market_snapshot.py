"""市場モデルの読取と入力固定。計算はmarket_context側へ委ねる。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
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
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_adjusted_bars
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.prices.price_reader import (
    read_latest_daily_bar_date,
)
from baibai_engine.machine_analysis.observations.stocks.contracts import MasterSnapshot
from baibai_engine.machine_analysis.observations.stocks.reader import read_master_snapshot
from baibai_engine.primitives.stocks import sector_33_peer_key
from baibai_engine.storage.sqlite.read import read_store_if_written
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import connect_current


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
            if (
                security.is_common_stock
                and (sector := sector_33_peer_key(security.sector_33)) is not None
            ):
                sectors.setdefault(sector, []).append(ticker)
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


class NoCachedDailyBars(ValueError):
    """There was no cached trading day at or before the requested ceiling."""


def build_market_snapshot(
    *,
    sqlite_path: Path,
    asof_date: date,
    history_weeks: int = DEFAULT_HISTORY_WEEKS,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
    min_breadth_sample: int = DEFAULT_MIN_BREADTH_SAMPLE,
    prepared: PreparedMarketContext | None = None,
    use_latest_cached_day: bool = False,
    current_master_only: bool = False,
) -> dict[str, object]:
    """Assemble the market snapshot as of ``asof_date``.

    ``points`` walks weekly evaluation dates from oldest to ``asof_date`` so a
    reader sees how trend and breadth evolved; ``sectors``
    aggregates per-sector returns and breadth at ``asof_date`` only. Market breadth
    uses common-stock membership at the benchmark's actual price date. A
    current-only generation also needs exact matching membership for sector metrics.
    A raw store read can instead resolve its latest cached price day through
    ``asof_date`` on the same snapshot as the observations.
    """
    if prepared is None:
        if current_master_only:
            raise ValueError("current market breadth requires a pinned market context")
        raw = read_store_if_written(
            sqlite_path,
            lambda: _read_snapshot_inputs(
                sqlite_path,
                asof_date=asof_date,
                history_weeks=history_weeks,
                benchmark_ticker=benchmark_ticker,
                use_latest_cached_day=use_latest_cached_day,
            ),
        )
        if raw is None:
            if use_latest_cached_day:
                raise NoCachedDailyBars
            raw = (asof_date, {}, None, {})
        asof_date, series_by_ticker, master, masters_by_date = raw
    else:
        if (
            use_latest_cached_day
            or prepared.sqlite_path != sqlite_path.resolve()
            or prepared.asof_date != asof_date
            or prepared.history_weeks != history_weeks
            or prepared.benchmark_ticker != benchmark_ticker
        ):
            raise ValueError("prepared market context does not match the requested scope")
        point_dates = _point_dates(asof_date, history_weeks)
        start = point_dates[0] - timedelta(days=_POINT_LOOKBACK_CALENDAR_DAYS)
        series_by_ticker = {
            ticker: series.since(start)
            for ticker, series in prepared.series_by_ticker.items()
            if series.dates and series.dates[-1] >= start
        }
        master = prepared.master
        masters_by_date = _breadth_masters(
            sqlite_path,
            point_dates=point_dates,
            benchmark=series_by_ticker.get(benchmark_ticker),
            asof_date=asof_date,
            master=master,
            current_master_only=current_master_only,
        )
    return calculate_market_snapshot(
        asof_date=asof_date,
        history_weeks=history_weeks,
        benchmark_ticker=benchmark_ticker,
        min_breadth_sample=min_breadth_sample,
        master=master,
        masters_by_date=masters_by_date,
        series_by_ticker=series_by_ticker,
        require_exact_sector_date=current_master_only,
    )


def _point_dates(asof_date: date, history_weeks: int) -> list[date]:
    return [asof_date - timedelta(weeks=offset) for offset in range(history_weeks - 1, -1, -1)]


type _SnapshotInputs = tuple[
    date, dict[str, _Series], MasterSnapshot | None, dict[date, MasterSnapshot | None]
]


def _breadth_masters(
    path: Path,
    *,
    point_dates: Sequence[date],
    benchmark: _Series | None,
    asof_date: date,
    master: MasterSnapshot | None,
    connection: sqlite3.Connection | None = None,
    current_master_only: bool = False,
) -> dict[date, MasterSnapshot | None]:
    if benchmark is None:
        return {}

    @cache
    def at_close(day: date) -> MasterSnapshot | None:
        if current_master_only:
            return master if master is not None and master.as_of == day else None
        # The master may already be bound by the Generation input.
        if day == asof_date:
            return master
        return read_master_snapshot(path, as_of=day, connection=connection)

    return {
        point: at_close(day)
        for point in point_dates
        if (day := benchmark.last_date_on(point)) is not None
    }


def _read_snapshot_inputs(
    path: Path,
    *,
    asof_date: date,
    history_weeks: int,
    benchmark_ticker: str,
    use_latest_cached_day: bool,
) -> _SnapshotInputs | None:
    # Inferred dates already required a written current store.
    connection = connect_current(path) if use_latest_cached_day else connect_read_only(path)
    if connection is None:
        return None
    with closing(connection):
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        if use_latest_cached_day:
            resolved = read_latest_daily_bar_date(connection, date.min, asof_date)
            if resolved is None:
                return None
            asof_date = resolved
        days = _point_dates(asof_date, history_weeks)
        start = days[0] - timedelta(days=_POINT_LOOKBACK_CALENDAR_DAYS)
        series = _load_series(connection, start=start, end=asof_date)
        master = read_master_snapshot(path, as_of=asof_date, connection=connection)
        masters = _breadth_masters(
            path,
            point_dates=days,
            benchmark=series.get(benchmark_ticker),
            asof_date=asof_date,
            master=master,
            connection=connection,
        )
    return asof_date, series, master, masters


def _load_series(connection: sqlite3.Connection, *, start: date, end: date) -> dict[str, _Series]:
    return {
        ticker: _Series(dates=[bar.traded_at for bar in bars], prices=[bar.price for bar in bars])
        for ticker, bars in read_adjusted_bars(connection, start=start, end=end).items()
    }
