"""個別市場モデルに必要な保存観測を固定し、計算へ渡す。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.machine_analysis.generation.runners.market_snapshot import PreparedMarketContext
from baibai_engine.machine_analysis.models.market_context.benchmark import NIKKEI225_ETF_PROXY
from baibai_engine.machine_analysis.models.market_context.security import (
    _PEER_RETURN_WINDOW_BARS,
    _window_endpoints,
    calculate_security_market_context,
)
from baibai_engine.machine_analysis.models.market_context.snapshot import (
    SECURITY_LOOKBACK_CALENDAR_DAYS,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar


# 現行個別市場観測の窓。旧較正の倍率履歴とは別の入力政策として保持する。
SELF_RANGE_WINDOW_BARS = 750


def build_security_market_contexts(
    *,
    sqlite_path: Path,
    tickers: Sequence[str],
    asof_date: date,
    self_range_window_bars: int,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
    prepared: PreparedMarketContext,
    price_bars_by_ticker: Mapping[str, Sequence[PriceBar]],
) -> dict[str, dict[str, object]]:
    """同じ生成で固定したmaster・共有価格だけから個別状態を返す。"""
    if (
        prepared.sqlite_path != sqlite_path.resolve()
        or prepared.asof_date != asof_date
        or prepared.benchmark_ticker != benchmark_ticker
    ):
        raise ValueError("prepared market context does not match the requested scope")
    start = asof_date - timedelta(days=SECURITY_LOOKBACK_CALENDAR_DAYS)
    contexts: dict[str, dict[str, object]] = {}
    for ticker in tickers:
        bars = [
            bar
            for bar in price_bars_by_ticker.get(ticker, ())
            if start <= bar.traded_at <= asof_date
        ]
        benchmark_prices, peer_prices = _comparison_inputs(
            ticker=ticker,
            bars=bars,
            prepared=prepared,
        )
        contexts[ticker] = calculate_security_market_context(
            ticker=ticker,
            bars=bars,
            master=prepared.master,
            benchmark=prepared.benchmark_trend,
            benchmark_ticker=benchmark_ticker,
            benchmark_prices=benchmark_prices,
            peer_prices=peer_prices,
            self_range_window_bars=self_range_window_bars,
        )
    return contexts


def _comparison_inputs(
    *,
    ticker: str,
    bars: Sequence[PriceBar],
    prepared: PreparedMarketContext,
) -> tuple[dict[date, float], list[dict[date, float]]]:
    if not bars:
        return {}, []
    benchmark_prices = prepared.benchmark_prices
    master = prepared.master
    security = None if master is None else master.securities.get(ticker)
    endpoints = _window_endpoints(bars, _PEER_RETURN_WINDOW_BARS)
    if security is None or endpoints is None:
        return benchmark_prices, []
    peer_prices = []
    for peer in prepared.common_stock_tickers_by_sector.get(security.sector_33, ()):
        if peer == ticker:
            continue
        series = prepared.series_by_ticker.get(peer)
        if series is not None:
            peer_prices.append(
                {day: value for day in endpoints if (value := series.price_on(day)) is not None}
            )
    return benchmark_prices, peer_prices
