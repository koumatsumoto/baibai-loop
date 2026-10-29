"""候補集合と独立した個別株の価格・市場/同業相対状態。"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import date

from baibai_engine.machine_analysis.models.market_context.benchmark import BenchmarkTrendSnapshot
from baibai_engine.machine_analysis.models.market_context.history import trailing_return
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.stocks.contracts import (
    MasterSnapshot,
)

_WEEK_52_WINDOW_BARS = 252
_VOL_WINDOW_BARS = 20
_TURNOVER_WINDOW_BARS = 20
_RETURN_WINDOWS_BARS = (1, 5, 20, 60)
_PEER_RETURN_WINDOW_BARS = 20


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


def _window_endpoints(bars: Sequence[PriceBar], window_bars: int) -> tuple[date, date] | None:
    if len(bars) < window_bars + 1:
        return None
    return bars[-(window_bars + 1)].traded_at, bars[-1].traded_at


def _endpoint_return(
    prices: Mapping[date, float], endpoints: tuple[date, date] | None
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


def calculate_security_market_context(
    *,
    ticker: str,
    bars: Sequence[PriceBar],
    master: MasterSnapshot | None,
    benchmark: BenchmarkTrendSnapshot | None,
    benchmark_ticker: str,
    benchmark_prices: Mapping[date, float],
    peer_prices: Sequence[Mapping[date, float]],
    self_range_window_bars: int,
) -> dict[str, object]:
    """固定した個社・市場・同業価格から比較を計算する。追加読取を行わない。"""
    security = None if master is None else master.securities.get(ticker)
    return {
        "master": None
        if master is None or security is None
        else {
            "name": security.name,
            "market_segment": security.market_segment,
            "sector_33": security.sector_33,
            "is_common_stock": security.is_common_stock,
            "snapshot_date": master.as_of.isoformat(),
        },
        "price": _price_block(bars, self_range_window_bars=self_range_window_bars),
        "relative": _relative_block(
            bars=bars,
            sector=None if security is None else security.sector_33,
            master=master,
            benchmark_ticker=benchmark_ticker,
            benchmark_prices=benchmark_prices,
            peer_prices=peer_prices,
        ),
        "benchmark_trend": None if benchmark is None else benchmark.to_dict(),
    }


def _relative_block(
    *,
    bars: Sequence[PriceBar],
    sector: str | None,
    master: MasterSnapshot | None,
    benchmark_ticker: str,
    benchmark_prices: Mapping[date, float],
    peer_prices: Sequence[Mapping[date, float]],
) -> dict[str, object] | None:
    if not bars:
        return None
    prices = [bar.price for bar in bars]
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
        "sector": _sector_block(bars=bars, sector=sector, master=master, peer_prices=peer_prices)
        if sector
        else None,
    }


def _sector_block(
    *,
    bars: Sequence[PriceBar],
    sector: str,
    master: MasterSnapshot | None,
    peer_prices: Sequence[Mapping[date, float]],
) -> dict[str, object] | None:
    endpoints = _window_endpoints(bars, _PEER_RETURN_WINDOW_BARS)
    if endpoints is None:
        return None
    returns = []
    for prices in peer_prices:
        peer_return = _endpoint_return(prices, endpoints)
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
