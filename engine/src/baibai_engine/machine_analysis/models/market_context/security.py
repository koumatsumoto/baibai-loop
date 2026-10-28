"""候補集合と独立した個別株の価格・市場/同業相対状態。"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.market.adjusted_prices import read_price_bars
from baibai_engine.market.jquants_reader import MasterSnapshot, read_master_snapshot
from baibai_engine.market.price_models import PriceBar

from baibai_engine.machine_analysis.models.market_context.benchmark import NIKKEI225_ETF_PROXY, compute_benchmark_trend
from baibai_engine.machine_analysis.models.market_context.history import trailing_return
from baibai_engine.machine_analysis.models.market_context.snapshot import SECURITY_LOOKBACK_CALENDAR_DAYS, PreparedMarketContext

_WEEK_52_WINDOW_BARS = 252
_VOL_WINDOW_BARS = 20
_TURNOVER_WINDOW_BARS = 20
_RETURN_WINDOWS_BARS = (1, 5, 20, 60)
_PEER_RETURN_WINDOW_BARS = 20


def build_security_market_context(
    *,
    sqlite_path: Path,
    ticker: str,
    asof_date: date,
    self_range_window_bars: int,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
    prepared: PreparedMarketContext | None = None,
) -> dict[str, object]:
    return build_security_market_contexts(
        sqlite_path=sqlite_path,
        tickers=(ticker,),
        asof_date=asof_date,
        self_range_window_bars=self_range_window_bars,
        benchmark_ticker=benchmark_ticker,
        prepared=prepared,
    )[ticker]


def build_security_market_contexts(
    *,
    sqlite_path: Path,
    tickers: Sequence[str],
    asof_date: date,
    self_range_window_bars: int,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
    prepared: PreparedMarketContext | None = None,
    price_bars_by_ticker: Mapping[str, Sequence[PriceBar]] | None = None,
) -> dict[str, dict[str, object]]:
    """渡された有限集合の長期価格を一括読取し、barsを保持せず個別状態を返す。"""
    if prepared is not None and (
        prepared.sqlite_path != sqlite_path.resolve()
        or prepared.asof_date != asof_date
        or prepared.benchmark_ticker != benchmark_ticker
    ):
        raise ValueError("prepared market context does not match the requested scope")
    start = asof_date - timedelta(days=SECURITY_LOOKBACK_CALENDAR_DAYS)
    bars_by_ticker = (
        read_price_bars(sqlite_path, tickers=tuple(tickers), start=start, end=asof_date)
        if price_bars_by_ticker is None
        else {
            ticker: [
                bar
                for bar in price_bars_by_ticker.get(ticker, ())
                if start <= bar.traded_at <= asof_date
            ]
            for ticker in tickers
        }
    )
    master = (
        read_master_snapshot(sqlite_path, as_of=asof_date) if prepared is None else prepared.master
    )
    benchmark = (
        compute_benchmark_trend(sqlite_path, asof_date, benchmark_ticker=benchmark_ticker)
        if prepared is None
        else prepared.benchmark_trend
    )
    contexts: dict[str, dict[str, object]] = {}
    for ticker in tickers:
        bars = bars_by_ticker.get(ticker, [])
        security = None if master is None else master.securities.get(ticker)
        sector = None if security is None else security.get("sector_33")
        contexts[ticker] = {
            "master": security,
            "price": _price_block(bars, self_range_window_bars=self_range_window_bars),
            "relative": _relative_block(
                sqlite_path,
                ticker=ticker,
                bars=bars,
                sector=sector if isinstance(sector, str) else None,
                master=master,
                asof_date=asof_date,
                benchmark_ticker=benchmark_ticker,
                prepared=prepared,
            ),
            "benchmark_trend": None if benchmark is None else benchmark.to_dict(),
        }
    return contexts


def _price_block(
    bars: Sequence[PriceBar], *, self_range_window_bars: int
) -> dict[str, object] | None:
    if not bars:
        return None
    prices = [bar.price for bar in bars]
    current = prices[-1]
    returns = {
        f"return_{window}d": trailing_return(prices, window) for window in _RETURN_WINDOWS_BARS
    }
    window_52w = prices[-_WEEK_52_WINDOW_BARS:]
    high_52w = max(window_52w)
    low_52w = min(window_52w)
    range_window = prices[-self_range_window_bars:]
    turnover = [
        bar.turnover_value
        for bar in bars[-_TURNOVER_WINDOW_BARS:]
        if bar.turnover_value is not None
    ]
    return {
        "resolved_date": bars[-1].traded_at.isoformat(),
        "close": current,
        "price_basis": "price_adjusted_close",
        **returns,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "gap_from_52w_low": (current / low_52w - 1) if low_52w else None,
        "gap_from_52w_high": (current / high_52w - 1) if high_52w else None,
        "self_range_percentile": (
            sum(1 for price in range_window if price <= current) / len(range_window)
        ),
        "self_range_window_bars": len(range_window),
        "realized_vol_20d": _realized_vol(prices),
        "avg_turnover_20d_oku": (
            statistics.fmean(turnover) / 1e8
            if len(turnover) >= _TURNOVER_WINDOW_BARS // 2
            else None
        ),
        "bar_count": len(bars),
    }


def _relative_block(
    sqlite_path: Path,
    *,
    ticker: str,
    bars: Sequence[PriceBar],
    sector: str | None,
    master: MasterSnapshot | None,
    asof_date: date,
    benchmark_ticker: str,
    prepared: PreparedMarketContext | None,
) -> dict[str, object] | None:
    if not bars:
        return None
    prices = [bar.price for bar in bars]
    start = bars[-min(len(bars), max(_RETURN_WINDOWS_BARS) + 1)].traded_at
    if prepared is not None and start >= prepared.benchmark_start:
        benchmark_prices = prepared.benchmark_prices
    else:
        benchmark_bars = read_price_bars(
            sqlite_path, tickers=(benchmark_ticker,), start=start, end=asof_date
        ).get(benchmark_ticker, [])
        benchmark_prices = {bar.traded_at: bar.price for bar in benchmark_bars}
    versus_benchmark = {}
    for window in _RETURN_WINDOWS_BARS:
        own = trailing_return(prices, window)
        bench = _endpoint_return(benchmark_prices, _window_endpoints(bars, window))
        versus_benchmark[f"relative_{window}d"] = (
            own - bench if own is not None and bench is not None else None
        )
    return {
        "benchmark_ticker": benchmark_ticker,
        **versus_benchmark,
        "sector": _sector_block(
            sqlite_path,
            ticker=ticker,
            bars=bars,
            sector=sector,
            asof_date=asof_date,
            master=master,
            prepared=prepared,
        )
        if sector
        else None,
    }


def _sector_block(
    sqlite_path: Path,
    *,
    ticker: str,
    bars: Sequence[PriceBar],
    sector: str,
    asof_date: date,
    master: MasterSnapshot | None,
    prepared: PreparedMarketContext | None,
) -> dict[str, object] | None:
    endpoints = _window_endpoints(bars, _PEER_RETURN_WINDOW_BARS)
    if endpoints is None:
        return None
    if prepared is not None:
        peers = [
            peer
            for peer in prepared.common_stock_tickers_by_sector.get(sector, ())
            if peer != ticker
        ]
    else:
        peers = (
            []
            if master is None
            else [
                peer
                for peer, security in master.securities.items()
                if peer != ticker
                and security["is_common_stock"]
                and security["sector_33"] == sector
            ]
        )
    if not peers:
        return None
    returns = []
    if prepared is not None and endpoints[0] >= prepared.start:
        for peer in peers:
            series = prepared.series_by_ticker.get(peer)
            if series is None:
                continue
            peer_return = _endpoint_return(
                {day: value for day in endpoints if (value := series.price_on(day)) is not None},
                endpoints,
            )
            if peer_return is not None:
                returns.append(peer_return)
    else:
        bars_by_ticker = read_price_bars(
            sqlite_path, tickers=tuple(peers), start=endpoints[0], end=asof_date
        )
        for peer_bars in bars_by_ticker.values():
            peer_return = _endpoint_return(
                {bar.traded_at: bar.price for bar in peer_bars}, endpoints
            )
            if peer_return is not None:
                returns.append(peer_return)
    if not returns:
        return None
    own_return = _endpoint_return({bar.traded_at: bar.price for bar in bars}, endpoints)
    peer_median = statistics.median(returns)
    return {
        "sector_33": sector,
        "peer_count": len(returns),
        "peer_median_return_20d": peer_median,
        "relative_return_20d": None if own_return is None else own_return - peer_median,
        "comparison_start_date": endpoints[0].isoformat(),
        "comparison_end_date": endpoints[1].isoformat(),
        "master_snapshot_date": None if master is None else master.as_of.isoformat(),
    }


def _window_endpoints(bars: Sequence[PriceBar], window_bars: int) -> tuple[date, date] | None:
    if len(bars) < window_bars + 1:
        return None
    return bars[-(window_bars + 1)].traded_at, bars[-1].traded_at


def _endpoint_return(
    prices: dict[date, float], endpoints: tuple[date, date] | None
) -> float | None:
    """Compare exact target dates; missing endpoints must not be filled."""
    if endpoints is None:
        return None
    start, end = (prices.get(endpoint) for endpoint in endpoints)
    if start is None or start == 0 or end is None:
        return None
    return end / start - 1


def _realized_vol(prices: Sequence[float]) -> float | None:
    window = prices[-(_VOL_WINDOW_BARS + 1) :]
    if len(window) < _VOL_WINDOW_BARS + 1:
        return None
    daily_returns = [
        window[index] / window[index - 1] - 1
        for index in range(1, len(window))
        if window[index - 1] != 0
    ]
    if len(daily_returns) < 2:
        return None
    return statistics.stdev(daily_returns) * math.sqrt(252)
