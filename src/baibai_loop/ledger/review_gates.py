from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from baibai_loop.date_utils import add_business_days

from .market_data import TrackingHorizon
from .trades import TradeRecord

_HORIZON_BUSINESS_DAYS: tuple[tuple[TrackingHorizon, int], ...] = (
    ("plus_15bd", 15),
    ("plus_30bd", 30),
)


@dataclass(frozen=True, slots=True)
class ReviewGate:
    trade_id: str
    ticker: str
    name: str
    horizon: TrackingHorizon
    entry_date: date
    target_date: date
    review_state: str
    is_due: bool


def due_review_gates(
    trades: Sequence[TradeRecord],
    asof: date,
    calendar: Sequence[date],
) -> list[ReviewGate]:
    """Return forward review gates whose target business day has arrived.

    A gate is *due* when its +Nbd target date is on or before ``asof`` while the
    trade has not yet recorded a completed review (``review_state != completed``).
    Gates with completed reviews or unresolved targets are not returned, so the
    output is the actionable backlog the review runbook must clear.
    """
    gates: list[ReviewGate] = []
    for trade in trades:
        if trade.review_state == "completed":
            continue
        for horizon, business_days in _HORIZON_BUSINESS_DAYS:
            target = add_business_days(trade.entry_date, business_days, calendar)
            if target is None or target > asof:
                continue
            gates.append(
                ReviewGate(
                    trade_id=trade.trade_id,
                    ticker=trade.ticker,
                    name=trade.name,
                    horizon=horizon,
                    entry_date=trade.entry_date,
                    target_date=target,
                    review_state=trade.review_state,
                    is_due=True,
                )
            )
    gates.sort(key=lambda gate: (gate.target_date, gate.ticker, gate.horizon))
    return gates


def weekday_calendar(start: date, end: date) -> list[date]:
    """Build a Monday-Friday calendar for ``[start, end]``.

    Fallback when the J-Quants market calendar is unavailable. It does not drop
    Japanese holidays, so a resolved target may land a holiday early; callers
    must treat it as an approximate backlog trigger, not a settlement date.
    """
    if end < start:
        return []
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days
