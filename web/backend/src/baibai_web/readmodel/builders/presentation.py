"""複数viewで共有するholding表示と基本的な値変換。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from baibai_engine.read_api import HoldingSnapshot
from baibai_web.readmodel.models import (
    HoldingView,
    ReservationView,
    UpcomingEventView,
)
from baibai_web.sources.types import ResearchRevision, ScreeningRunRecord

_POSITION_REVIEW_NULL_REASONS = {
    "investment_case_uncertain": "投資仮説の成立が未確認です",
    "primary_evidence_requires_verification": "投資仮説の一次根拠が未確認です",
    "valuation_unresolved": "企業価値の算定が未確定です",
    "valuation_basis_requires_refresh": "企業評価の基準日が判断日と異なります",
    "quote_missing": "対象日の株価がありません",
    "quote_stale": "株価が鮮度上限を超えています",
    "quote_after_judgment": "株価の日付が判断日より後です",
    "quantity_basis_unconfirmed": "株式単位・権利変化が未確認です",
    "remaining_reward_requires_confirmation": "残存見返りの判断が未確認です",
}


def position_review_reason_text(reason: str | None) -> str | None:
    return None if reason is None else _POSITION_REVIEW_NULL_REASONS.get(reason, reason)


_EVENT_WINDOW_DAYS = 14

_EVENT_KIND_ORDER = {"earnings": 0, "reservation_expiry": 1}

_JST = ZoneInfo("Asia/Tokyo")


def latest_research_by_ticker(
    revisions: list[ResearchRevision],
) -> dict[str, ResearchRevision]:
    latest: dict[str, ResearchRevision] = {}
    for revision in revisions:
        latest.setdefault(revision.ticker, revision)
    return latest


def security_names_for_run(run: ScreeningRunRecord | None) -> dict[str, str]:
    if run is None:
        return {}
    result: dict[str, str] = {}
    for row in run.rows:
        ticker = text(row.get("ticker"))
        name = text(row.get("name"))
        if ticker is not None and name is not None:
            result[ticker] = name
    return result


def holding_view(
    holding: HoldingSnapshot,
    *,
    revision: ResearchRevision | None,
    security_name: str | None,
    next_earnings_date: date | None = None,
) -> HoldingView:
    price_value = None if holding.market_price_yen is None else float(holding.market_price_yen)
    price_display = None if holding.market_price_yen is None else str(holding.market_price_yen)
    market_value = holding.market_value_yen
    price_as_of = holding.market_price_observed_at
    pnl = None if market_value is None else market_value - holding.deployed_cost_yen
    fair_value = revision.pmax_raw_yen if revision is not None else None
    fv_gap = (
        round((fair_value - price_value) / price_value * 100, 1)
        if fair_value is not None and price_value is not None and price_value != 0
        else None
    )
    return HoldingView(
        ticker=holding.ticker,
        company_name=revision.company_name if revision is not None else security_name,
        sector=holding.sector,
        quantity=holding.quantity,
        deployed_cost_yen=holding.deployed_cost_yen,
        market_price_yen=price_display,
        market_price_as_of=price_as_of,
        market_value_yen=market_value,
        unrealized_pnl_yen=pnl,
        unrealized_pnl_pct=percentage(pnl, holding.deployed_cost_yen, digits=2),
        pmax_raw_yen=fair_value,
        pmax_gap_pct=fv_gap,
        latest_thesis_id=revision.thesis_id if revision is not None else None,
        disposition=revision.disposition if revision is not None else None,
        next_earnings_date=(
            next_earnings_date.isoformat() if next_earnings_date is not None else None
        ),
    )


def upcoming_events(
    *,
    today: date,
    holdings: list[HoldingView],
    reservations: list[ReservationView],
) -> list[UpcomingEventView]:
    """Collapse holding earnings and reservation expiries into one chronological list
    within the next ``_EVENT_WINDOW_DAYS`` days.

    The window is inclusive on both ends: an event dated today (days_until 0) through
    ``today + _EVENT_WINDOW_DAYS`` is surfaced; anything past or beyond is dropped so the
    The app only shows what needs attention now.
    """

    window_end = today + timedelta(days=_EVENT_WINDOW_DAYS)
    events: list[UpcomingEventView] = []
    for holding in holdings:
        if holding.next_earnings_date is None:
            continue
        event_date = date.fromisoformat(holding.next_earnings_date)
        if today <= event_date <= window_end:
            events.append(
                UpcomingEventView(
                    event_date=event_date,
                    kind="earnings",
                    ticker=holding.ticker,
                    label=holding.company_name or holding.ticker,
                    days_until=(event_date - today).days,
                )
            )
    for reservation in reservations:
        event_date = (reservation.expires_at - timedelta(microseconds=1)).date()
        if today <= event_date <= window_end:
            events.append(
                UpcomingEventView(
                    event_date=event_date,
                    kind="reservation_expiry",
                    ticker=reservation.ticker,
                    label=reservation.ticker,
                    days_until=(event_date - today).days,
                )
            )
    events.sort(key=lambda item: (item.event_date, _EVENT_KIND_ORDER[item.kind], item.ticker or ""))
    return events


def percentage(numerator: int | None, denominator: int | None, *, digits: int) -> float | None:
    if numerator is None or denominator is None:
        return None
    return round(numerator / denominator * 100, digits) if denominator else 0.0


def number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return float(parsed) if parsed.is_finite() else None


def text(value: object) -> str | None:
    return value if isinstance(value, str) else None
