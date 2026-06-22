from __future__ import annotations

import sqlite3
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from baibai_loop.foundation.coerce import optional_float
from baibai_loop.screening.providers.jquants import JQuantsDailyBar

from .benchmark import NIKKEI225_ETF_PROXY
from .tracking import resolve_price_on_or_before

# Forward horizons for recommended-queue evaluation, in calendar weeks. The
# target date is asof + weeks*7 days, resolved to the latest bar on or before it
# (so a weekend/holiday target falls back to the prior trading day). Horizons
# whose target is after the cached price coverage are reported as unresolved
# rather than silently dropped, so replay weeks too recent for a horizon do not
# masquerade as a zero return.
DEFAULT_HORIZON_WEEKS: tuple[int, ...] = (1, 4)


@dataclass(frozen=True, slots=True)
class HorizonReturn:
    weeks: int
    target_date: date
    resolved: bool
    price: float | None
    return_ratio: float | None
    relative: float | None


@dataclass(frozen=True, slots=True)
class TickerForwardReturn:
    ticker: str
    asof: date
    entry_price: float | None
    horizons: tuple[HorizonReturn, ...]


def latest_bar_date(bars: Sequence[JQuantsDailyBar]) -> date | None:
    """Return the most recent traded date present in ``bars`` (the eval cap)."""
    return max((bar.traded_at for bar in bars), default=None)


def compute_ticker_forward_returns(
    ticker: str,
    asof: date,
    bars: Sequence[JQuantsDailyBar],
    *,
    eval_cap: date,
    horizon_weeks: Sequence[int] = DEFAULT_HORIZON_WEEKS,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
) -> TickerForwardReturn:
    """Compute forward return at each horizon for one recommended ticker.

    Entry and forward prices share the basis of :func:`resolve_price_on_or_before`
    (adjusted close preferred), so a corporate action inside the window does not
    distort the return. ``relative`` subtracts the benchmark proxy return over the
    same window.
    """
    entry = resolve_price_on_or_before(ticker, asof, bars)
    entry_price = entry.price if entry is not None else None
    horizons: list[HorizonReturn] = []
    for weeks in horizon_weeks:
        target = asof + timedelta(days=weeks * 7)
        if target > eval_cap:
            horizons.append(
                HorizonReturn(
                    weeks=weeks,
                    target_date=target,
                    resolved=False,
                    price=None,
                    return_ratio=None,
                    relative=None,
                )
            )
            continue
        forward = resolve_price_on_or_before(ticker, target, bars)
        benchmark_return = _benchmark_return(benchmark_ticker, asof, target, bars)
        price = forward.price if forward is not None else None
        return_ratio = (
            (price - entry_price) / entry_price
            if price is not None and entry_price is not None and entry_price != 0
            else None
        )
        relative = (
            return_ratio - benchmark_return
            if return_ratio is not None and benchmark_return is not None
            else None
        )
        horizons.append(
            HorizonReturn(
                weeks=weeks,
                target_date=target,
                resolved=True,
                price=price,
                return_ratio=return_ratio,
                relative=relative,
            )
        )
    return TickerForwardReturn(
        ticker=ticker,
        asof=asof,
        entry_price=entry_price,
        horizons=tuple(horizons),
    )


@dataclass(frozen=True, slots=True)
class HorizonAggregate:
    weeks: int
    count: int
    mean_return: float | None
    median_return: float | None
    mean_relative: float | None


def aggregate_forward_returns(
    results: Sequence[TickerForwardReturn],
    horizon_weeks: Sequence[int] = DEFAULT_HORIZON_WEEKS,
) -> list[HorizonAggregate]:
    """Aggregate per-ticker forward returns into per-horizon mean/median.

    Only resolved horizons with a computed return contribute, so a recommended
    ticker that lacks price coverage at a horizon lowers ``count`` instead of
    biasing the mean toward zero.
    """
    aggregates: list[HorizonAggregate] = []
    for weeks in horizon_weeks:
        returns = [
            horizon.return_ratio
            for result in results
            for horizon in result.horizons
            if horizon.weeks == weeks and horizon.resolved and horizon.return_ratio is not None
        ]
        relatives = [
            horizon.relative
            for result in results
            for horizon in result.horizons
            if horizon.weeks == weeks and horizon.resolved and horizon.relative is not None
        ]
        aggregates.append(
            HorizonAggregate(
                weeks=weeks,
                count=len(returns),
                mean_return=statistics.fmean(returns) if returns else None,
                median_return=statistics.median(returns) if returns else None,
                mean_relative=statistics.fmean(relatives) if relatives else None,
            )
        )
    return aggregates


def _benchmark_return(
    benchmark_ticker: str,
    asof: date,
    target: date,
    bars: Sequence[JQuantsDailyBar],
) -> float | None:
    entry = resolve_price_on_or_before(benchmark_ticker, asof, bars)
    forward = resolve_price_on_or_before(benchmark_ticker, target, bars)
    if entry is None or forward is None or entry.price == 0:
        return None
    return forward.price / entry.price - 1


def load_bars_for_tickers(sqlite_path: Path, tickers: set[str]) -> tuple[JQuantsDailyBar, ...]:
    """Load all cached daily bars for ``tickers`` from the screening SQLite store."""
    if not tickers or not sqlite_path.exists():
        return ()
    # placeholders is only "?,?,..." markers; ticker values are bound parameters
    # in conn.execute, so the f-string is not an injection vector. bandit cannot
    # see the binding, so suppress its B608 false positive here.
    placeholders = ",".join("?" for _ in tickers)
    query = (
        "SELECT ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor "  # nosec B608
        "FROM jquants_daily_bars "
        f"WHERE ticker IN ({placeholders}) ORDER BY ticker, traded_at"
    )
    conn = sqlite3.connect(sqlite_path)
    try:
        rows = conn.execute(query, tuple(sorted(tickers))).fetchall()
    finally:
        conn.close()
    bars: list[JQuantsDailyBar] = []
    for ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor in rows:
        if close is None or traded_at is None:
            continue
        bars.append(
            JQuantsDailyBar(
                ticker=str(ticker),
                traded_at=date.fromisoformat(traded_at),
                close=float(close),
                turnover_value=optional_float(turnover_value),
                adjustment_close=optional_float(adjustment_close),
                adjustment_factor=optional_float(adjustment_factor),
            )
        )
    return tuple(bars)


def format_pct(value: float | None) -> str:
    return f"{value * 100:+.1f}" if value is not None else "n/a"
