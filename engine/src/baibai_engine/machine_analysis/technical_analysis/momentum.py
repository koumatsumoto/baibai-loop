"""共通の取引日を端点とする価格変化。銘柄の欠測で比較期間をずらさない。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Literal

from baibai_engine.market.price_models import PriceBar


@dataclass(frozen=True, slots=True)
class PriceMomentum:
    status: Literal["available", "insufficient_calendar", "missing_endpoint", "invalid_price"]
    reference_date: date | None
    start_date: date | None
    end_date: date | None
    start_price: float | None
    end_price: float | None
    value: float | None


def price_momentum_12_1(
    adjusted_bars: Sequence[PriceBar], *, trading_dates: Sequence[date], as_of: date
) -> PriceMomentum:
    """P(t-21) / P(t-252) - 1。価格は同一調整基準、日付は市場共通の営業日。

    配当込みリターンではない。途中の終値欠測だけでは端点比較を無効にしない。
    調整係数の検証と原本の選択は呼出元が担い、ここでは再取得や補完をしない。
    """
    dates = sorted({day for day in trading_dates if day <= as_of})
    reference = dates[-1] if dates else None
    if len(dates) < 253:
        return PriceMomentum("insufficient_calendar", reference, None, None, None, None, None)
    start, end = dates[-253], dates[-22]
    endpoints: dict[date, float] = {}
    for bar in adjusted_bars:
        if bar.traded_at not in (start, end):
            continue
        if bar.traded_at in endpoints:
            raise ValueError("momentum requires one price per endpoint")
        endpoints[bar.traded_at] = bar.price
    first, last = endpoints.get(start), endpoints.get(end)
    if first is None or last is None:
        return PriceMomentum("missing_endpoint", reference, start, end, first, last, None)
    if any(not isfinite(price) or price <= 0 for price in (first, last)):
        return PriceMomentum("invalid_price", reference, start, end, first, last, None)
    value = last / first - 1
    if not isfinite(value):
        return PriceMomentum("invalid_price", reference, start, end, first, last, None)
    return PriceMomentum("available", reference, start, end, first, last, value)
