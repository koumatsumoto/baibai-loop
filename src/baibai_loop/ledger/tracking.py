from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from baibai_loop.date_utils import add_business_days
from baibai_loop.screening.providers.jquants import JQuantsDailyBar

from .market_data import PriceObservation, TrackingHorizon


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
    decision_event_id: str,
    decision_date: date,
    calendar: Sequence[date],
    bars: Sequence[JQuantsDailyBar],
    fallback_observations: Sequence[PriceObservation] = (),
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
    if price_15 is None:
        price_15 = _fallback_tracking_price(
            ticker,
            decision_event_id=decision_event_id,
            target=plus_15_target,
            horizon="plus_15bd",
            observations=fallback_observations,
        )
    if price_30 is None:
        price_30 = _fallback_tracking_price(
            ticker,
            decision_event_id=decision_event_id,
            target=plus_30_target,
            horizon="plus_30bd",
            observations=fallback_observations,
        )
    return price_15, price_30


def _fallback_tracking_price(
    ticker: str,
    *,
    decision_event_id: str,
    target: date | None,
    horizon: TrackingHorizon,
    observations: Sequence[PriceObservation],
) -> TrackingPrice | None:
    candidates = [
        observation
        for observation in observations
        if observation.ticker == ticker
        and observation.decision_event_id == decision_event_id
        and observation.tracking_horizon == horizon
        and (target is None or observation.target_date == target)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda observation: (
            observation.provisional,
            observation.resolved_trade_date,
        )
    )
    observation = candidates[0]
    return TrackingPrice(price=observation.price, source=observation.source_payload())
