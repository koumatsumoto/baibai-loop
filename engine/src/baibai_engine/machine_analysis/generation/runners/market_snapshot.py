"""市場モデルの読取と入力固定。計算はmarket_context側へ委ねる。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.machine_analysis.models.market_context.benchmark import (
    DEFAULT_MIN_BREADTH_SAMPLE,
    NIKKEI225_ETF_PROXY,
)
from baibai_engine.machine_analysis.models.market_context.snapshot import (
    _POINT_LOOKBACK_CALENDAR_DAYS,
    DEFAULT_HISTORY_WEEKS,
    PreparedMarketContext,
    _Series,
    calculate_market_snapshot,
)
from baibai_engine.machine_analysis.models.market_context.snapshot import (
    prepare_market_context_from_bars as prepare_from_bars,
)
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.stocks.contracts import MasterSnapshot
from baibai_engine.machine_analysis.observations.stocks.reader import read_master_snapshot


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
    return prepare_from_bars(
        sqlite_path=sqlite_path.resolve(),
        asof_date=asof_date,
        master=master,
        bars_by_ticker=bars_by_ticker,
        subjects=subjects,
        history_weeks=history_weeks,
        benchmark_ticker=benchmark_ticker,
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
