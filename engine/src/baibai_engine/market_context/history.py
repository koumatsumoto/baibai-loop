"""価格騰落・volatility・流動性・split状態の純粋算術。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from math import sqrt
from statistics import mean

from baibai_engine.market.bars import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
    asof_basis_closes,
)

PRICE_HISTORY_WINDOW_DAYS = 750
AVG_VOLUME_SESSIONS = 20
AVG_VOLUME_MIN_OBSERVED = 15


def trailing_return(prices: Sequence[float], window_bars: int) -> float | None:
    if len(prices) < window_bars + 1:
        return None
    past = prices[-(window_bars + 1)]
    if past == 0:
        return None
    return prices[-1] / past - 1


def latest_bar_on_or_before(
    bars: Sequence[JQuantsDailyBar],
    asof_date: date,
) -> JQuantsDailyBar | None:
    filtered = [bar for bar in bars if bar.traded_at <= asof_date]
    return max(filtered, key=lambda item: item.traded_at) if filtered else None


def price_change(
    bars: Sequence[JQuantsDailyBar],
    sessions: int,
    asof_date: date,
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar] | None = None,
) -> float | None:
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if len(ordered) <= sessions:
        return None
    # 分割を跨ぐ比較で偽の騰落を出さないよう、adjustment_factor から組んだ
    # asof 基準系列で両端を比較する (cache の adjustment_close は基準混在のため不使用)。
    prices = asof_basis_closes(ordered[-(sessions + 1) :], adjustment_events, asof_date=asof_date)
    return trailing_return(prices, sessions)


def realized_volatility(
    bars: Sequence[JQuantsDailyBar],
    sessions: int,
    asof_date: date,
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar] | None = None,
) -> float | None:
    """Annualized close-to-close volatility on one as-of-consistent share basis."""
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if len(ordered) <= sessions:
        return None
    prices = asof_basis_closes(ordered[-(sessions + 1) :], adjustment_events, asof_date=asof_date)
    returns = [
        prices[index] / prices[index - 1] - 1.0
        for index in range(1, len(prices))
        if prices[index - 1] > 0
    ]
    if len(returns) != sessions or len(returns) < 2:
        return None
    mean_return = mean(returns)
    variance = sum((value - mean_return) ** 2 for value in returns) / (len(returns) - 1)
    return sqrt(variance * 252)


def gap_from_low(
    bars: Sequence[JQuantsDailyBar],
    sessions: int,
    asof_date: date,
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar] | None = None,
) -> float | None:
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if not ordered:
        return None
    prices = asof_basis_closes(ordered[-sessions:], adjustment_events, asof_date=asof_date)
    current = prices[-1]
    low = min(prices)
    if low <= 0:
        return None
    return (current / low) - 1.0


def avg_daily_volume(
    bars: Sequence[JQuantsDailyBar],
    asof_date: date,
    sessions: int = AVG_VOLUME_SESSIONS,
) -> float | None:
    """Mean traded shares over the trailing sessions, or None when too sparse.

    Shares rather than yen, because it is the denominator that turns a margin
    balance into days of trading; dividing yen turnover by the close would put the
    close where the day's average price belongs. The window must exist in full so a
    newly listed name cannot produce a days-of-volume figure off two sessions, but
    within it a minority of unreported days is averaged over rather than fatal.
    """
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    window = ordered[-sessions:]
    if len(window) < sessions:
        return None
    values = [float(bar.volume) for bar in window if bar.volume is not None]
    if len(values) < AVG_VOLUME_MIN_OBSERVED:
        return None
    return mean(values)


def turnover_spike(
    bars: Sequence[JQuantsDailyBar],
    asof_date: date,
    latest_sessions: int = 5,
    baseline_sessions: int = 20,
) -> float | None:
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if len(ordered) < latest_sessions + baseline_sessions:
        return None
    latest_values = [
        bar.turnover_value for bar in ordered[-latest_sessions:] if bar.turnover_value is not None
    ]
    baseline_values = [
        bar.turnover_value
        for bar in ordered[-(latest_sessions + baseline_sessions) : -latest_sessions]
        if bar.turnover_value is not None
    ]
    if len(latest_values) < latest_sessions or len(baseline_values) < baseline_sessions:
        return None
    baseline = mean(baseline_values)
    if baseline <= 0:
        return None
    return mean(latest_values) / baseline


def has_split_adjustment_within_sessions(
    bars: Sequence[JQuantsDailyBar],
    asof_date: date,
    sessions: int,
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar] | None = None,
) -> bool:
    """True when J-Quants adjustment_factor marks a split in the latest sessions.

    J-Quants daily_quotes sets ``AdjustmentFactor`` on ex-rights dates for
    stock splits and reverse splits. Use the same session window as
    ``price_change_60d`` so the flag covers the price-change calculation it
    qualifies.
    """
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if len(ordered) < 2:
        return False

    relevant = ordered[-(sessions + 1) :]
    start = relevant[0].traded_at
    events = bars if adjustment_events is None else adjustment_events
    factors = [
        event.adjustment_factor
        for event in events
        if start <= event.traded_at <= asof_date and event.adjustment_factor is not None
    ]
    return any(factor != 1.0 for factor in factors) or len(set(factors)) > 1
