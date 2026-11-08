"""L2 price and share-unit transformations over received action values."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from math import isfinite

from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
)


def asof_basis_prices(
    prices: Sequence[tuple[date, float]],
    adjustment_events: Sequence[tuple[date, float | None]],
    *,
    asof_date: date,
) -> list[float]:
    """観測価格と独立factorからas-of基準の価格を組む、共有価格算術。

    callerはasof以下の価格を日付昇順で渡す。出力はその価格列と同じ順序になる。
    factor側の日付制限と整列はこの関数が行う。
    価格換算に使う係数は有限の正数でなければならない。0を調整なしとは扱わない。
    """
    if not prices:
        return []
    events = sorted(
        (
            event
            for event in adjustment_events
            if event[1] not in (None, 1.0) and prices[0][0] < event[0] <= asof_date
        ),
        key=lambda event: event[0],
    )
    result = [0.0] * len(prices)
    factor = 1.0
    event_index = len(events) - 1
    for index in range(len(prices) - 1, -1, -1):
        traded_at, close = prices[index]
        while event_index >= 0 and events[event_index][0] > traded_at:
            adjustment = events[event_index][1]
            assert adjustment is not None
            if not isfinite(adjustment) or adjustment <= 0:
                raise ValueError("price adjustment factor must be finite and positive")
            factor *= adjustment
            event_index -= 1
        result[index] = close * factor
    return result


def share_unit_factor(event: JQuantsAdjustmentFactorEvent | JQuantsDailyBar) -> float | None:
    """Return a share-unit conversion, not the provider's price adjustment.

    ExRT 1/2 identifies splits/reverse splits; 3 is a rights issue and does not
    rescale an existing share. A nontrivial price factor without a recognized kind
    cannot establish a share-unit conversion. Missing ordinary observations do not
    invent an action; completeness remains the source reader's responsibility.
    """
    factor = event.adjustment_factor
    if event.ex_right_type == "3":
        return 1.0
    if event.ex_right_type in ("1", "2"):
        return factor if factor is not None and factor > 0 else None
    if event.ex_right_type is None and factor in (None, 1.0):
        return 1.0
    return None


def cumulative_share_unit_factor_after(
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    after: date,
    asof_date: date,
    *,
    include_boundary_day: bool = False,
) -> float | None:
    """Accumulate only known share-unit changes in the requested interval.

    Rights-issue price factors do not scale shares or cash DPS. Unknown action
    kinds leave the unit basis unresolved. Disclosure-day inclusion follows the
    existing boundary convention; it does not prove a filing's share basis.
    """
    factor = 1.0
    for bar in ticker_bars:
        before_start = bar.traded_at < after if include_boundary_day else bar.traded_at <= after
        if before_start or bar.traded_at > asof_date:
            continue
        adjustment = share_unit_factor(bar)
        if adjustment is None:
            return None
        factor *= adjustment
    return factor
