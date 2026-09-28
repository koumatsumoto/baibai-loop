"""価格履歴からvaluation・出来高・volatilityを導出する。"""

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
from baibai_engine.screening.schema import (
    FinancialSnapshot,
)

# 自己レンジ / sigma gap が前提にする約 3 年の価格履歴窓(暦日)。listing 起点の
# short_history_flag では検出できない「上場は古いが bar 履歴に長期ギャップがある」
# 銘柄を、窓内の bar 本数(対 population 最大比)として事実記録するために使う。
PRICE_HISTORY_WINDOW_DAYS = 750

# valuation history の自己レンジに使う直近 bar 本数(立会日)。上の窓が暦日なのに対し
# こちらは session 数で、同じ 750 でも表す量が違う。
VALUATION_HISTORY_SESSIONS = 750


def _latest_bar_on_or_before(
    bars: Sequence[JQuantsDailyBar],
    asof_date: date,
) -> JQuantsDailyBar | None:
    filtered = [bar for bar in bars if bar.traded_at <= asof_date]
    return max(filtered, key=lambda item: item.traded_at) if filtered else None


def _valuation_history(
    latest_price: float,
    bars: Sequence[JQuantsDailyBar],
    snapshot: FinancialSnapshot,
    asof_date: date,
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    history_sessions: int = VALUATION_HISTORY_SESSIONS,
) -> dict[str, list[float]]:
    # asof 以前の bar に限定し、look-ahead bias を防ぐ。
    eligible = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date),
        key=lambda item: item.traded_at,
    )
    # 価格の不連続 (分割) を valuation history に持ち込まないため調整済み系列を使う。
    # cache の adjustment_close は incremental 取得で基準が混在するため使わず、
    # 不変イベントの adjustment_factor から asof 基準の系列を自前で組む。
    prices = asof_basis_closes(eligible[-history_sessions:], adjustment_events, asof_date=asof_date)
    ev_ebitda_history: list[float] = []
    if (
        snapshot.shares_ex_treasury is not None
        and snapshot.debt is not None
        and snapshot.cash is not None
        and snapshot.ebitda_ttm is not None
        and snapshot.ebitda_ttm > 0
    ):
        ev_ebitda_history = [
            value
            for price in prices
            if (value := _historical_ev_ebitda(price, snapshot)) is not None
        ]
    pbr_history: list[float] = []
    pbr = snapshot.pbr
    if pbr is not None and pbr != 0:
        pbr_basis = latest_price / pbr
        pbr_history = [price / pbr_basis for price in prices]
    p_s_history: list[float] = []
    if (
        snapshot.sales_ttm is not None
        and snapshot.sales_ttm > 0
        and snapshot.shares_ex_treasury is not None
    ):
        p_s_history = [
            (price * snapshot.shares_ex_treasury) / snapshot.sales_ttm for price in prices
        ]

    # Historical forward PER holds forecast EPS constant against adjusted close,
    # matching the trailing-PER history convention and keeping range metrics
    # available for the forward valuation input.
    per_forward_history: list[float] = []
    if snapshot.per_forward is not None and snapshot.per_forward != 0:
        per_forward_basis = latest_price / snapshot.per_forward
        if per_forward_basis > 0:
            per_forward_history = [price / per_forward_basis for price in prices]
    history: dict[str, list[float]] = {
        "per_forward": per_forward_history,
        "per_trailing": [
            (price / snapshot.eps) for price in prices if snapshot.eps and snapshot.eps > 0
        ],
        "pbr": pbr_history,
        "ev_ebitda": ev_ebitda_history,
        "p_s": p_s_history,
    }
    return history


def _historical_ev_ebitda(
    price: float,
    snapshot: FinancialSnapshot,
) -> float | None:
    """Return the EV/EBITDA value for one historical price.

    Caller must ensure ``shares_ex_treasury`` / ``debt`` / ``cash`` are not
    None and ``ebitda_ttm`` is positive. v1 has only the latest balance sheet
    and TTM EBITDA, so those are held constant across price history while
    market cap varies with the (adjusted) historical close.

    Negative EV or non-positive EBITDA is outside the valuation multiple
    domain and is handled by cash / net-cash Valuation Approachs, not by EV/EBITDA mean
    reversion.
    """
    shares_ex_treasury = snapshot.shares_ex_treasury
    debt = snapshot.debt
    cash = snapshot.cash
    ebitda_ttm = snapshot.ebitda_ttm
    if shares_ex_treasury is None or debt is None or cash is None:
        raise ValueError("EV/EBITDA history requires shares, debt, and cash")
    if ebitda_ttm is None or ebitda_ttm <= 0:
        raise ValueError("EV/EBITDA history requires positive EBITDA")
    enterprise_value = (price * shares_ex_treasury) + debt - cash
    if enterprise_value <= 0:
        return None
    return enterprise_value / ebitda_ttm


def _price_change(
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
    base = prices[0]
    if base == 0:
        return None
    return (prices[-1] / base) - 1.0


def _realized_volatility(
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


def _gap_from_low(
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


# A trailing window must be long enough to average out one busy day, and it has to
# tolerate the days an illiquid name simply does not report. Demanding all twenty
# would drop the established thin names where margin overhang matters most.
AVG_VOLUME_SESSIONS = 20

AVG_VOLUME_MIN_OBSERVED = 15


def _avg_daily_volume(
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


def _turnover_spike(
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


def _has_split_adjustment_within_sessions(
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


def _self_range_percentile(history: Sequence[float], current: float | None) -> float | None:
    if not history or current is None:
        return None
    low = min(history)
    high = max(history)
    if high == low:
        return 0.0
    return (current - low) / (high - low)


def _sigma_gap(history: Sequence[float], current: float | None) -> float | None:
    if current is None or len(history) < 2:
        return None
    avg = mean(history)
    variance = sum((value - avg) ** 2 for value in history) / len(history)
    stddev = sqrt(variance)
    if stddev == 0:
        return 0.0
    return (current - avg) / stddev
