from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from baibai_loop.date_utils import add_business_days
from baibai_loop.screening.providers.jquants import JQuantsDailyBar


def resolve_price_on_or_before(
    ticker: str,
    target: date,
    bars: Sequence[JQuantsDailyBar],
) -> tuple[float | None, bool]:
    candidates = [bar for bar in bars if bar.ticker == ticker and bar.traded_at <= target]
    if not candidates:
        return None, False
    latest = max(candidates, key=lambda bar: bar.traded_at)
    if latest.adjustment_close is not None:
        return latest.adjustment_close, latest.adjustment_close != latest.close
    return latest.close, False


def resolve_tracking_prices(
    ticker: str,
    decision_date: date,
    calendar: Sequence[date],
    bars: Sequence[JQuantsDailyBar],
) -> tuple[float | None, float | None]:
    plus_15 = add_business_days(decision_date, 15, calendar)
    plus_30 = add_business_days(decision_date, 30, calendar)
    price_15 = resolve_price_on_or_before(ticker, plus_15, bars)[0] if plus_15 else None
    price_30 = resolve_price_on_or_before(ticker, plus_30, bars)[0] if plus_30 else None
    return price_15, price_30
