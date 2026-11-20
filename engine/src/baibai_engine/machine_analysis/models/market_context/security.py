"""候補集合と独立した個別株の価格・市場/同業相対状態。"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import date

from baibai_engine.machine_analysis.models.market_context.history import trailing_return
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.prices.price_observation import PriceCalendar
from baibai_engine.machine_analysis.observations.stocks.contracts import (
    MasterSnapshot,
)
from baibai_engine.primitives.stocks import sector_33_peer_key

_WEEK_52_WINDOW_BARS = 252
_VOL_CALENDAR_DIFFERENCES = 20
_TURNOVER_WINDOW_BARS = 20
_RETURN_WINDOWS_BARS = (1, 5, 20, 60)
_PEER_RETURN_WINDOW_BARS = 20


def _price_block(bars: Sequence[PriceBar], *, calendar: PriceCalendar) -> dict[str, object] | None:
    if not bars:
        return None
    # 高安・騰落率は実在bar。年率変動は別の共通営業日窓をすべて必要とする。
    prices = [bar.price for bar in bars[-_WEEK_52_WINDOW_BARS:]]
    current = prices[-1]
    returns = {
        f"return_{window}d": trailing_return(prices, window) for window in _RETURN_WINDOWS_BARS
    }
    valid_range = all(math.isfinite(price) and price > 0 for price in prices)
    high_52w = max(prices) if valid_range else None
    low_52w = min(prices) if valid_range else None
    turnover_bars = bars[-_TURNOVER_WINDOW_BARS:]
    turnover = [
        bar.turnover_value
        for bar in turnover_bars
        if bar.turnover_value is not None
        and math.isfinite(bar.turnover_value)
        and bar.turnover_value >= 0
    ]
    volatility, volatility_context = _realized_vol(bars, calendar=calendar)
    return {
        "resolved_date": bars[-1].traded_at.isoformat(),
        "close": current if math.isfinite(current) and current > 0 else None,
        "price_basis": "price_adjusted_close",
        **returns,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "gap_from_52w_low": (current / low_52w - 1) if low_52w is not None else None,
        "gap_from_52w_high": (current / high_52w - 1) if high_52w is not None else None,
        "realized_vol_20d": volatility,
        "realized_vol_20d_context": volatility_context,
        "avg_turnover_20d_oku": (
            statistics.fmean(turnover) / 1e8
            if len(turnover) >= _TURNOVER_WINDOW_BARS // 2
            else None
        ),
        "turnover_window": {
            "start_date": turnover_bars[0].traded_at.isoformat(),
            "end_date": turnover_bars[-1].traded_at.isoformat(),
            "price_bar_count": len(turnover_bars),
            "valid_turnover_count": len(turnover),
            "median_yen": statistics.median(turnover)
            if len(turnover) >= _TURNOVER_WINDOW_BARS // 2
            else None,
        },
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
    return price_return(prices.get(endpoints[0]), prices.get(endpoints[1]))


def price_return(start: float | None, end: float | None) -> float | None:
    """Return on the two supplied endpoints; do not seek another day."""
    if (
        start is None
        or end is None
        or not all(math.isfinite(price) and price > 0 for price in (start, end))
    ):
        return None
    value = end / start - 1
    return value if math.isfinite(value) else None


def _realized_vol(
    bars: Sequence[PriceBar], *, calendar: PriceCalendar
) -> tuple[float | None, dict[str, object]]:
    window = calendar.windows[_VOL_CALENDAR_DIFFERENCES]
    prices = {bar.traded_at: bar.price for bar in bars}
    valid = (
        []
        if window is None
        else [
            prices[day]
            for day in window
            if day in prices and math.isfinite(prices[day]) and prices[day] > 0
        ]
    )
    reason = (
        "insufficient_calendar"
        if window is None
        else "incomplete_close_window"
        if len(valid) != len(window)
        else None
    )
    context: dict[str, object] = {
        "window_basis": "common_trading_calendar",
        "calendar_differences": _VOL_CALENDAR_DIFFERENCES,
        "window_start_date": None if window is None else window[0].isoformat(),
        "window_end_date": None if window is None else window[-1].isoformat(),
        "valid_close_count": None if window is None else len(valid),
        "return_basis": "simple_adjusted_close_return",
        "standard_deviation_ddof": 1,
        "annualization_sessions": 252,
        "unavailable_reason": reason,
    }
    if reason is not None:
        return None, context
    # No shorter sample, zero fill or older bar may turn a multi-session gap
    # into one daily return before the sqrt(252) annualization.
    daily_returns = [valid[index] / valid[index - 1] - 1 for index in range(1, len(valid))]
    if any(not math.isfinite(value) for value in daily_returns):
        context["unavailable_reason"] = "nonfinite_returns"
        return None, context
    value = statistics.stdev(daily_returns) * math.sqrt(252)
    if not math.isfinite(value):
        context["unavailable_reason"] = "nonfinite_volatility"
        return None, context
    return value, context


def calculate_security_market_context(
    *,
    ticker: str,
    bars: Sequence[PriceBar],
    calendar: PriceCalendar,
    master: MasterSnapshot | None,
    benchmark_ticker: str,
    benchmark_prices: Mapping[date, float],
    peer_returns: Sequence[float],
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
        "price": _price_block(bars, calendar=calendar),
        "relative": _relative_block(
            bars=bars,
            sector=None if security is None else security.sector_33,
            master=master,
            benchmark_ticker=benchmark_ticker,
            benchmark_prices=benchmark_prices,
            peer_returns=peer_returns,
        ),
    }


def _relative_block(
    *,
    bars: Sequence[PriceBar],
    sector: str | None,
    master: MasterSnapshot | None,
    benchmark_ticker: str,
    benchmark_prices: Mapping[date, float],
    peer_returns: Sequence[float],
) -> dict[str, object] | None:
    if not bars:
        return None
    prices = [bar.price for bar in bars]
    versus_benchmark = {}
    comparison_windows = {}
    for window in _RETURN_WINDOWS_BARS:
        endpoints = _window_endpoints(bars, window)
        own = trailing_return(prices, window)
        bench = _endpoint_return(benchmark_prices, endpoints)
        name = f"relative_{window}d"
        versus_benchmark[name] = own - bench if own is not None and bench is not None else None
        comparison_windows[name] = (
            None
            if endpoints is None
            else {"start_date": endpoints[0].isoformat(), "end_date": endpoints[1].isoformat()}
        )
    peer_sector = sector_33_peer_key(sector)
    return {
        "benchmark_ticker": benchmark_ticker,
        **versus_benchmark,
        "window_basis": "observed_security_bar_differences",
        "comparison_windows": comparison_windows,
        "sector": _sector_block(
            bars=bars, sector=peer_sector, master=master, peer_returns=peer_returns
        )
        if peer_sector is not None
        else None,
    }


def _sector_block(
    *,
    bars: Sequence[PriceBar],
    sector: str,
    master: MasterSnapshot | None,
    peer_returns: Sequence[float],
) -> dict[str, object] | None:
    endpoints = _window_endpoints(bars, _PEER_RETURN_WINDOW_BARS)
    if endpoints is None or not peer_returns:
        return None
    peer_median = statistics.median(peer_returns)
    own_return = _endpoint_return({bar.traded_at: bar.price for bar in bars}, endpoints)
    return {
        "sector_33": sector,
        "peer_count": len(peer_returns),
        "peer_median_return_20d": peer_median,
        "relative_return_20d": None if own_return is None else own_return - peer_median,
        "comparison_start_date": endpoints[0].isoformat(),
        "comparison_end_date": endpoints[1].isoformat(),
        "master_snapshot_date": None if master is None else master.as_of.isoformat(),
    }
