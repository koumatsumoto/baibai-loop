from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Literal

from baibai_loop.foundation.date_utils import add_business_days
from baibai_loop.market.price_walk import (
    TrackingPrice as TrackingPrice,
)
from baibai_loop.market.price_walk import (
    resolve_price_on_or_before as resolve_price_on_or_before,
)
from baibai_loop.screening.providers.jquants import JQuantsDailyBar

type TrackingHorizon = Literal["plus_15bd", "plus_30bd"]


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
