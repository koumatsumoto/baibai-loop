"""複数viewで共有するholding表示と基本的な値変換。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from baibai_engine.read_api import HoldingSnapshot
from baibai_web.readmodel.models import HoldingView
from baibai_web.sources.protocols import MarketPriceSource
from baibai_web.sources.types import ResearchRevision, ScreeningRunRecord

_JST = ZoneInfo("Asia/Tokyo")


def reservation_last_valid_day(expires_at: datetime) -> date:
    """Display an exclusive reservation deadline on its last valid JST day."""
    return (expires_at.astimezone(_JST) - timedelta(microseconds=1)).date()


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
    market: MarketPriceSource,
    revision: ResearchRevision | None,
    security_name: str | None,
    next_earnings_date: date | None = None,
) -> HoldingView:
    price_value = None if holding.market_price_yen is None else float(holding.market_price_yen)
    price_display = None if holding.market_price_yen is None else str(holding.market_price_yen)
    market_value = holding.market_value_yen
    price_as_of = holding.market_price_observed_at
    pnl = None if market_value is None else market_value - holding.deployed_cost_yen
    fair_value = revision.pmax_original_yen if revision is not None else None
    fv_gap = None
    gap_unavailable_reason = None
    if fair_value is not None and price_value is not None and price_value != 0:
        assert revision is not None
        if (
            revision.original_price_as_of is None
            or price_as_of is None
            or not market.price_comparison_basis_is_confirmed(
                holding.ticker,
                from_date=revision.original_price_as_of,
                through_date=price_as_of.astimezone(_JST).date(),
            )
        ):
            gap_unavailable_reason = "原評価から現在株価までの株式単位を確認できません。"
        else:
            fv_gap = round((fair_value - price_value) / price_value * 100, 1)
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
        pmax_gap_unavailable_reason=gap_unavailable_reason,
        latest_assessment_id=revision.assessment_id if revision is not None else None,
        stance=revision.stance if revision is not None else None,
        next_earnings_date=(
            next_earnings_date.isoformat() if next_earnings_date is not None else None
        ),
    )


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
