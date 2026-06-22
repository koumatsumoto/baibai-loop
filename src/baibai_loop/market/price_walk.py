from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from baibai_loop.market.bars import JQuantsDailyBar


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
