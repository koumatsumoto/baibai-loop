"""新規購入工程で企業評価と現在の価格・保有・資本条件の誤判断を止める。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from baibai_engine.research.thesis import ThesisDocument, current_price_projection
from baibai_engine.research.valuation import finite_decimal, maximum_entry_price


@dataclass(frozen=True, slots=True)
class EntryResult:
    eligible: bool
    reasons: tuple[str, ...]
    maximum_price_yen: Decimal | None
    quantity: int
    current_price_projection: dict[str, object] | None = None


def evaluate_entry(
    thesis: ThesisDocument,
    *,
    reviewed: bool,
    latest: bool,
    as_of: date,
    price_yen: Decimal | None,
    price_as_of: date | None,
    basis_confirmed: bool,
    minimum_required_annual_return_pct: Decimal,
    market_price_max_age_days: int,
    held_quantity: int,
    active_reservation: bool,
    assessment_executed: bool,
    available_cash_yen: Decimal,
    budget_max_yen: Decimal,
    board_lot: object,
) -> EntryResult:
    """One current rule shared by CAA and Planning; no persistence or broker fact input."""
    reasons: list[str] = []
    if not reviewed:
        reasons.append("reviewed_thesis_required")
    if not latest:
        reasons.append("latest_thesis_required")
    if thesis.judgment.disposition != "candidate" or thesis.investment_case.status != "intact":
        reasons.append("thesis_not_candidate")
    if thesis.input_snapshot.as_of != as_of:
        reasons.append("valuation_basis_requires_refresh")
    if held_quantity > 0:
        reasons.append("already_held")
    if active_reservation:
        reasons.append("active_reservation_exists")
    if assessment_executed:
        reasons.append("assessment_already_executed")
    if not basis_confirmed:
        reasons.append("corporate_action_unresolved")
    if price_as_of is None or not 0 <= (as_of - price_as_of).days <= market_price_max_age_days:
        reasons.append("quote_missing_or_stale")
    price = None if price_yen is None else finite_decimal(price_yen)
    if price is None or price <= 0:
        reasons.append("positive_quote_required")
    valuation = thesis.valuation
    maximum = None
    if (
        valuation.base is None
        or valuation.horizon_months is None
        or valuation.required_annual_return_pct is None
    ):
        reasons.append("valuation_unresolved")
    else:
        maximum = maximum_entry_price(
            valuation.base,
            horizon_months=valuation.horizon_months,
            required_annual_return_pct=valuation.required_annual_return_pct,
        )
        if valuation.required_annual_return_pct < finite_decimal(
            minimum_required_annual_return_pct
        ):
            reasons.append("required_return_below_current_policy")
        if price is not None and price > maximum:
            reasons.append("price_above_pmax")
    cash, guide = finite_decimal(available_cash_yen), finite_decimal(budget_max_yen)
    if (
        isinstance(board_lot, bool)
        or not isinstance(board_lot, int)
        or board_lot <= 0
        or guide <= 0
    ):
        raise ValueError("positive board_lot and budget guide required")
    quantity = 0
    if price is not None and price > 0:
        lot_cost = price * board_lot
        cash_lots = max(0, int(cash // lot_cost))
        guide_lots = max(1, int(guide // lot_cost))
        quantity = min(cash_lots, guide_lots) * board_lot
    if quantity == 0:
        reasons.append("available_cash_below_board_lot")
    projection = (
        current_price_projection(
            thesis,
            price_yen=price,
            price_as_of=price_as_of,
            as_of=as_of,
            basis_confirmed=reviewed and latest and basis_confirmed,
            max_quote_age_days=market_price_max_age_days,
        )
        if price is not None and price > 0
        else None
    )
    return EntryResult(not reasons, tuple(reasons), maximum, 0 if reasons else quantity, projection)
