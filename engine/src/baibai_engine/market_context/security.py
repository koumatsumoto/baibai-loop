"""候補集合と独立した個別株の価格・市場/同業相対状態。"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from .benchmark import NIKKEI225_ETF_PROXY, compute_benchmark_trend
from .history import trailing_return
from .master import MasterSnapshot, read_master_snapshot
from .prices import PriceBar, read_price_bars
from .snapshot import PreparedMarketContext

_WEEK_52_WINDOW_BARS = 252
_VOL_WINDOW_BARS = 20
_TURNOVER_WINDOW_BARS = 20
_RETURN_WINDOWS_BARS = (1, 5, 20, 60)
_PEER_RETURN_WINDOW_BARS = 20
_TICKER_LOOKBACK_CALENDAR_DAYS = 1130


def build_security_market_context(
    *,
    sqlite_path: Path,
    ticker: str,
    asof_date: date,
    self_range_window_bars: int,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
    prepared: PreparedMarketContext | None = None,
) -> dict[str, object]:
    if prepared is not None and (
        prepared.sqlite_path != sqlite_path.resolve()
        or prepared.asof_date != asof_date
        or prepared.benchmark_ticker != benchmark_ticker
    ):
        raise ValueError("prepared market context does not match the requested scope")
    bars = read_price_bars(
        sqlite_path,
        tickers=(ticker,),
        start=asof_date - timedelta(days=_TICKER_LOOKBACK_CALENDAR_DAYS),
        end=asof_date,
    ).get(ticker, [])
    master = (
        read_master_snapshot(sqlite_path, as_of=asof_date) if prepared is None else prepared.master
    )
    security = None if master is None else master.securities.get(ticker)
    sector = None if security is None else security.get("sector_33")
    benchmark = (
        compute_benchmark_trend(sqlite_path, asof_date, benchmark_ticker=benchmark_ticker)
        if prepared is None
        else prepared.benchmark_trend
    )
    return {
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
        "bollinger_20bar": _bollinger_20bar(bars),
    }


def _bollinger_20bar(bars: Sequence[PriceBar]) -> dict[str, object]:
    """Describe the last 20 observed closes, not value or reversal probability."""
    window = bars[-20:]
    result: dict[str, object] = {
        "status": "insufficient_history",
        "window_bars": len(window),
        "window_start_date": window[0].traded_at.isoformat() if window else None,
        "window_end_date": window[-1].traded_at.isoformat() if window else None,
        "z_score": None,
        "position": None,
    }
    if len(window) < 20:
        return result
    prices = [bar.price for bar in window]
    if any(not math.isfinite(price) or price <= 0 for price in prices):
        return {**result, "status": "invalid_price"}
    mean = statistics.fmean(prices)
    sigma = statistics.pstdev(prices)
    if sigma == 0:
        return {**result, "status": "zero_variance"}
    current = prices[-1]
    return {
        **result,
        "status": "available",
        "z_score": (current - mean) / sigma,
        "position": "above_upper"
        if current > mean + 2 * sigma
        else "below_lower"
        if current < mean - 2 * sigma
        else "within_bands",
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
    peers = (
        []
        if master is None
        else [
            peer
            for peer, security in master.securities.items()
            if peer != ticker and security["is_common_stock"] and security["sector_33"] == sector
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
