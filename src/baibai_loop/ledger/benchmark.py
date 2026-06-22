from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from baibai_loop.market.benchmark import (
    NIKKEI225_ETF_PROXY as NIKKEI225_ETF_PROXY,
)
from baibai_loop.market.benchmark import (
    _benchmark_return,
    _price_on_or_before,
)
from baibai_loop.screening.providers.jquants import JQuantsDailyBar

from .trades import TradeRecord


@dataclass(frozen=True, slots=True)
class TradeBenchmark:
    ticker: str
    name: str
    entry_date: date
    quantity: int
    entry_price: float
    eval_price: float | None
    eval_date: date
    gross_pnl: float | None
    return_ratio: float | None
    benchmark_return: float | None
    relative: float | None


@dataclass(frozen=True, slots=True)
class PortfolioBenchmark:
    asof: date
    benchmark_ticker: str
    total_notional: float
    total_gross_pnl: float
    portfolio_return: float | None
    benchmark_return: float | None
    relative: float | None
    positions: tuple[TradeBenchmark, ...]
    warnings: tuple[str, ...]


def compute_forward_performance(
    trades: Sequence[TradeRecord],
    asof: date,
    bars: Sequence[JQuantsDailyBar],
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
) -> PortfolioBenchmark:
    """Compute per-position and portfolio forward return versus a benchmark proxy.

    Stock and benchmark prices both resolve to the latest bar on or before the
    relevant date via :func:`resolve_price_on_or_before`, so they share one basis.
    The portfolio benchmark return is weighted by entry notional to match how a
    monthly retro aggregates positions entered on different dates.
    """
    warnings: list[str] = []
    positions: list[TradeBenchmark] = []
    weighted_benchmark = 0.0
    total_notional = 0.0
    total_gross_pnl = 0.0
    benchmark_complete = True
    for trade in trades:
        notional = trade.entry_price * trade.quantity
        eval_price = _price_on_or_before(trade.ticker, asof, bars)
        benchmark_return = _benchmark_return(benchmark_ticker, trade.entry_date, asof, bars)
        gross_pnl: float | None = None
        return_ratio: float | None = None
        relative: float | None = None
        if eval_price is None:
            warnings.append(f"no price on or before {asof.isoformat()} for {trade.ticker}")
        else:
            gross_pnl = (eval_price - trade.entry_price) * trade.quantity
            return_ratio = (eval_price - trade.entry_price) / trade.entry_price
            total_gross_pnl += gross_pnl
        if benchmark_return is None:
            benchmark_complete = False
            warnings.append(
                f"no benchmark {benchmark_ticker} price for entry {trade.entry_date.isoformat()}"
            )
        else:
            weighted_benchmark += benchmark_return * notional
        if return_ratio is not None and benchmark_return is not None:
            relative = return_ratio - benchmark_return
        total_notional += notional
        positions.append(
            TradeBenchmark(
                ticker=trade.ticker,
                name=trade.name,
                entry_date=trade.entry_date,
                quantity=trade.quantity,
                entry_price=trade.entry_price,
                eval_price=eval_price,
                eval_date=asof,
                gross_pnl=gross_pnl,
                return_ratio=return_ratio,
                benchmark_return=benchmark_return,
                relative=relative,
            )
        )
    portfolio_return = total_gross_pnl / total_notional if total_notional > 0 else None
    benchmark_return = (
        weighted_benchmark / total_notional if total_notional > 0 and benchmark_complete else None
    )
    relative = (
        portfolio_return - benchmark_return
        if portfolio_return is not None and benchmark_return is not None
        else None
    )
    return PortfolioBenchmark(
        asof=asof,
        benchmark_ticker=benchmark_ticker,
        total_notional=total_notional,
        total_gross_pnl=total_gross_pnl,
        portfolio_return=portfolio_return,
        benchmark_return=benchmark_return,
        relative=relative,
        positions=tuple(positions),
        warnings=tuple(warnings),
    )
