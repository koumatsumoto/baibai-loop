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
    price_return,
)
from baibai_engine.machine_analysis.models.market_context.snapshot import (
    SECURITY_LOOKBACK_CALENDAR_DAYS,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.prices.price_observation import PriceCalendar


def build_security_market_contexts(
    *,
    sqlite_path: Path,
    tickers: Sequence[str],
    asof_date: date,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
    prepared: PreparedMarketContext,
    price_bars_by_ticker: Mapping[str, Sequence[PriceBar]],
    calendar: PriceCalendar,
) -> dict[str, dict[str, object]]:
    """同じ生成で固定したmaster・共有価格だけから個別状態を返す。"""
    if (
        prepared.sqlite_path != sqlite_path.resolve()
        or prepared.asof_date != asof_date
        or prepared.benchmark_ticker != benchmark_ticker
        or calendar.as_of != asof_date
    ):
        raise ValueError("prepared market context does not match the requested scope")
    start = asof_date - timedelta(days=SECURITY_LOOKBACK_CALENDAR_DAYS)

    # Bound retained peer values by the master population even when illiquid
    # targets have many different pairs of endpoints. A miss recomputes exactly.
    samples: dict[str, tuple[tuple[date, date], dict[str, float]]] = {}
    contexts: dict[str, dict[str, object]] = {}
    for ticker in tickers:
        bars = [
            bar
            for bar in price_bars_by_ticker.get(ticker, ())
            if start <= bar.traded_at <= asof_date
        ]
        security = prepared.master.securities.get(ticker) if prepared.master is not None else None
        endpoints = _window_endpoints(bars, _PEER_RETURN_WINDOW_BARS)
        peers = []
        if security is not None and endpoints is not None:
            sector = security.sector_33
            sample = samples.get(sector)
            if sample is None or sample[0] != endpoints:
                values = {}
                for peer in prepared.common_stock_tickers_by_sector.get(sector, ()):
                    series = prepared.series_by_ticker.get(peer)
                    if series is not None:
                        value = price_return(
                            series.price_on(endpoints[0]), series.price_on(endpoints[1])
                        )
                        if value is not None:
                            values[peer] = value
                sample = samples[sector] = endpoints, values
            peers = [value for peer, value in sample[1].items() if peer != ticker]
        contexts[ticker] = calculate_security_market_context(
            ticker=ticker,
            bars=bars,
            master=prepared.master,
            calendar=calendar,
            benchmark_ticker=benchmark_ticker,
            benchmark_prices=prepared.benchmark_prices,
            peer_returns=peers,
        )
    return contexts
