"""旧較正の価格感応倍率履歴・自己レンジ。現在財務を固定するreplay契約。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    FinancialSnapshot,
)
from baibai_engine.machine_analysis.observations.prices.price_basis import asof_basis_closes
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
)

VALUATION_HISTORY_SESSIONS = 750


def build_valuation_history(
    bars: Sequence[JQuantsDailyBar],
    snapshot: FinancialSnapshot,
    asof_date: date,
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    history_sessions: int = VALUATION_HISTORY_SESSIONS,
) -> dict[str, list[float]]:
    # asof より後の価格barを除く。配信元の後日訂正まで復元するものではない。
    eligible = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date),
        key=lambda item: item.traded_at,
    )
    # 分割・併合・ライツイシューを含む価格factorで、同一asof基準の系列を組む。
    # 係数にも配信元の訂正があるため、永久に不変のイベントとは扱わない。
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
    latest_price = snapshot.market_price_yen
    pbr = snapshot.pbr
    if latest_price is not None and latest_price > 0 and pbr is not None and pbr != 0:
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
    if (
        latest_price is not None
        and latest_price > 0
        and snapshot.per_forward is not None
        and snapshot.per_forward != 0
    ):
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
    None and ``ebitda_ttm`` is positive. The current snapshot inputs
    are held constant across price history while
    market cap varies with the (adjusted) historical close.

    Negative EV or non-positive EBITDA is outside the valuation multiple
    domain and is handled by cash / net-cash Valuation Approaches, not by EV/EBITDA mean
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


def self_range_position(history: Sequence[float], current: float | None) -> float | None:
    """Position between historical extrema, not an observation-rank percentile."""
    if not history or current is None:
        return None
    low = min(history)
    high = max(history)
    if high == low:
        return 0.0
    return (current - low) / (high - low)
