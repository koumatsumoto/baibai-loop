"""個別市場モデルに必要な保存観測を固定し、計算へ渡す。"""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.stocks.contracts import MasterSnapshot
from baibai_engine.machine_analysis.observations.stocks.reader import read_master_snapshot
from baibai_engine.machine_analysis.models.market_context.benchmark import NIKKEI225_ETF_PROXY
from baibai_engine.machine_analysis.models.market_context.snapshot import SECURITY_LOOKBACK_CALENDAR_DAYS, PreparedMarketContext
from baibai_engine.machine_analysis.models.market_context.security import (
    _RETURN_WINDOWS_BARS, _PEER_RETURN_WINDOW_BARS, _window_endpoints,
    calculate_security_market_context,
)
from .benchmark import compute_benchmark_trend


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
        benchmark_prices, peer_prices = _comparison_inputs(
            sqlite_path=sqlite_path, ticker=ticker, bars=bars, master=master,
            asof_date=asof_date, benchmark_ticker=benchmark_ticker, prepared=prepared,
        )
        contexts[ticker] = calculate_security_market_context(
            ticker=ticker, bars=bars, master=master, benchmark=benchmark,
            benchmark_ticker=benchmark_ticker, benchmark_prices=benchmark_prices,
            peer_prices=peer_prices, self_range_window_bars=self_range_window_bars,
        )
    return contexts


def _comparison_inputs(
    *, sqlite_path: Path, ticker: str, bars: Sequence[PriceBar],
    master: MasterSnapshot | None, asof_date: date, benchmark_ticker: str,
    prepared: PreparedMarketContext | None,
) -> tuple[dict[date, float], list[dict[date, float]]]:
    if not bars:
        return {}, []
    start = bars[-min(len(bars), max(_RETURN_WINDOWS_BARS) + 1)].traded_at
    if prepared is not None and start >= prepared.benchmark_start:
        benchmark_prices = prepared.benchmark_prices
    else:
        benchmark_bars = read_price_bars(
            sqlite_path, tickers=(benchmark_ticker,), start=start, end=asof_date
        ).get(benchmark_ticker, [])
        benchmark_prices = {bar.traded_at: bar.price for bar in benchmark_bars}
    security = None if master is None else master.securities.get(ticker)
    sector = None if security is None else security.get("sector_33")
    endpoints = _window_endpoints(bars, _PEER_RETURN_WINDOW_BARS)
    if not sector or not isinstance(sector, str) or endpoints is None:
        return benchmark_prices, []
    if prepared is not None:
        peers = [peer for peer in prepared.common_stock_tickers_by_sector.get(sector, ()) if peer != ticker]
    else:
        peers = [] if master is None else [
            peer for peer, row in master.securities.items()
            if peer != ticker and row["is_common_stock"] and row["sector_33"] == sector
        ]
    if not peers:
        return benchmark_prices, []
    if prepared is not None and endpoints[0] >= prepared.start:
        peer_prices = []
        for peer in peers:
            series = prepared.series_by_ticker.get(peer)
            if series is not None:
                peer_prices.append({day: value for day in endpoints if (value := series.price_on(day)) is not None})
    else:
        peer_bars = read_price_bars(sqlite_path, tickers=tuple(peers), start=endpoints[0], end=asof_date)
        peer_prices = [{bar.traded_at: bar.price for bar in values} for values in peer_bars.values()]
    return benchmark_prices, peer_prices

