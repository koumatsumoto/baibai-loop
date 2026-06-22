from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from baibai_loop.foundation.date_utils import add_business_days
from baibai_loop.screening.providers.jquants import JQuantsDailyBar

type TrackingHorizon = Literal["plus_15bd", "plus_30bd"]


@dataclass(frozen=True, slots=True)
class TrackingPrice:
    price: float
    source: dict[str, object]


def resolve_price_on_or_before(
    ticker: str,
    target: date,
    bars: Sequence[JQuantsDailyBar],
) -> TrackingPrice | None:
    candidates = [bar for bar in bars if bar.ticker == ticker and bar.traded_at <= target]
    if not candidates:
        return None
    latest = max(candidates, key=lambda bar: bar.traded_at)
    if latest.adjustment_close is not None:
        return TrackingPrice(
            price=latest.adjustment_close,
            source={
                "source_kind": "jquants",
                "resolved_trade_date": latest.traded_at.isoformat(),
                "price_basis": "adjusted_close",
                "provisional": False,
            },
        )
    return TrackingPrice(
        price=latest.close,
        source={
            "source_kind": "jquants",
            "resolved_trade_date": latest.traded_at.isoformat(),
            "price_basis": "close_unadjusted",
            "provisional": False,
        },
    )


def resolve_tracking_prices(
    ticker: str,
    decision_date: date,
    calendar: Sequence[date],
    bars: Sequence[JQuantsDailyBar],
) -> tuple[TrackingPrice | None, TrackingPrice | None]:
    plus_15_target = add_business_days(decision_date, 15, calendar) if calendar else None
    plus_30_target = add_business_days(decision_date, 30, calendar) if calendar else None
    price_15 = (
        resolve_price_on_or_before(ticker, plus_15_target, bars)
        if plus_15_target and bars
        else None
    )
    price_30 = (
        resolve_price_on_or_before(ticker, plus_30_target, bars)
        if plus_30_target and bars
        else None
    )
    return price_15, price_30
