"""保有の表示と日付・比率の変換。"""

from datetime import date, datetime, timedelta

from baibai_engine.portfolio.ledger.events import HoldingSnapshot, ValuationYen, round_valuation_yen
from baibai_engine.primitives.time import JST as _JST
from baibai_web.readmodel.models import HoldingView


def reservation_last_valid_day(expires_at: datetime) -> date:
    """Display an exclusive reservation deadline on its last valid JST day."""
    return (expires_at.astimezone(_JST) - timedelta(microseconds=1)).date()


def holding_view(holding: HoldingSnapshot, *, security_name: str | None) -> HoldingView:
    value = holding.market_value_yen
    displayed_value = round_valuation_yen(value)
    return HoldingView(
        ticker=holding.ticker,
        company_name=security_name,
        sector=holding.sector,
        quantity=holding.quantity,
        deployed_cost_yen=holding.deployed_cost_yen,
        market_price_yen=None
        if holding.market_price_yen is None
        else str(holding.market_price_yen),
        market_price_as_of=holding.market_price_as_of,
        market_value_yen=displayed_value,
        unrealized_pnl_yen=None
        if displayed_value is None
        else displayed_value - holding.deployed_cost_yen,
        unrealized_pnl_pct=percentage(
            None if value is None else value - holding.deployed_cost_yen,
            holding.deployed_cost_yen,
            digits=2,
        ),
    )


def percentage(
    numerator: ValuationYen | None, denominator: ValuationYen | None, *, digits: int
) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return round(float(numerator / denominator * 100), digits)
