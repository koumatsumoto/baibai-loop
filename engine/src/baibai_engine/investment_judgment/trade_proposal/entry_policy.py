"""新規購入工程で銘柄評価と現在の価格・保有・資本条件の整合を確認する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    current_price_projection,
)
from baibai_engine.investment_judgment.stock_assessment.scenario import maximum_entry_price
from baibai_engine.investment_judgment.trade_proposal.contracts import GateReason, QuoteInput
from baibai_engine.primitives.numbers import finite_decimal

type BuyGateCode = Literal[
    "reviewed_assessment_required",
    "latest_assessment_required",
    "assessment_not_supported",
    "valuation_basis_requires_refresh",
    "already_held",
    "active_reservation_exists",
    "proposal_already_executed",
    "corporate_action_unresolved",
    "quote_missing_or_stale",
    "positive_quote_required",
    "valuation_unresolved",
    "positive_limit_required",
    "limit_above_pmax",
    "available_cash_below_board_lot",
    "selected_quantity_above_maximum",
]


@dataclass(frozen=True, slots=True)
class BuyTermsEvaluation:
    hard_blockers: tuple[GateReason, ...]
    maximum_price_yen: Decimal | None
    quantity: int
    maximum_quantity: int
    current_price_above_maximum: bool | None
    current_price_projection: dict[str, object] | None
    limit_price_projection: dict[str, object] | None


def _positive_board_lot(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("positive board_lot and budget guide required")
    return value


def _selected_board_lot_quantity(value: object, board_lot: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value % board_lot:
        raise ValueError("selected_quantity must be a positive board-lot multiple")
    return value


def evaluate_buy_terms(
    assessment: StockAssessmentDocument,
    *,
    reviewed: bool,
    latest: bool,
    as_of: date,
    quote: QuoteInput | None,
    proposed_limit_yen: Decimal,
    basis_confirmed: bool,
    market_price_max_age_days: int,
    held_quantity: int,
    active_reservation: bool,
    proposal_executed: bool,
    available_cash_yen: Decimal,
    budget_max_yen: Decimal,
    board_lot: int,
    selected_quantity: int | None = None,
) -> BuyTermsEvaluation:
    """実quoteでgateを確認し、作者limitで上限と選択数量を分ける。"""
    if quote is not None and quote.ticker != assessment.input_snapshot.ticker:
        raise ValueError("quote and assessment ticker differ")
    limit = finite_decimal(proposed_limit_yen)
    cash, guide = finite_decimal(available_cash_yen), finite_decimal(budget_max_yen)
    board_lot = _positive_board_lot(board_lot)
    if guide <= 0:
        raise ValueError("positive board_lot and budget guide required")
    if selected_quantity is not None:
        selected_quantity = _selected_board_lot_quantity(selected_quantity, board_lot)
    blockers: list[GateReason] = []

    def block(code: BuyGateCode, message: str) -> None:
        blockers.append(GateReason(code=code, message=message))

    if not reviewed:
        block("reviewed_assessment_required", "独立 Review 済みの評価が必要です。")
    if not latest:
        block("latest_assessment_required", "最新の公開評価で再確認してください。")
    if assessment.conclusion.stance != "supported" or assessment.investment_case.status != "intact":
        block("assessment_not_supported", "supported かつ intact の評価が必要です。")
    if assessment.input_snapshot.as_of != as_of:
        block("valuation_basis_requires_refresh", "期間と今後分配を本日起点で再確認してください。")
    if held_quantity > 0:
        block("already_held", "既保有銘柄の新規買付は初期 policy の対象外です。")
    if active_reservation:
        block("active_reservation_exists", "既存の active 予約を確認してください。")
    if proposal_executed:
        block("proposal_already_executed", "この提案に対する買付は既に記録されています。")
    confirmed_basis = basis_confirmed and quote is not None
    if not confirmed_basis:
        block("corporate_action_unresolved", "株式単位と価格の basis を確認してください。")
    price = None if quote is None else finite_decimal(quote.price_yen)
    price_as_of = None if quote is None else quote.price_as_of
    if price_as_of is None or not 0 <= (as_of - price_as_of).days <= market_price_max_age_days:
        block("quote_missing_or_stale", "現在 quote が欠測、未来日、または鮮度期限外です。")
    if price is None or price <= 0:
        block("positive_quote_required", "正の実 quote が必要です。")
    valuation = assessment.valuation
    maximum = None
    if (
        valuation.status != "resolved"
        or valuation.base is None
        or valuation.horizon_months is None
        or valuation.required_annual_return_pct is None
    ):
        block("valuation_unresolved", "Base、期間、要求率の確定が必要です。")
    else:
        maximum = maximum_entry_price(
            valuation.base,
            horizon_months=valuation.horizon_months,
            required_annual_return_pct=valuation.required_annual_return_pct,
        )
        if limit > maximum:
            block("limit_above_pmax", "提案 limit が Base による購入上限を超えています。")
    maximum_quantity = 0
    if limit <= 0:
        block("positive_limit_required", "提案 limit は正の価格で指定してください。")
    else:
        lot_cost = limit * board_lot
        cash_lots = max(0, int(cash // lot_cost))
        guide_lots = max(1, int(guide // lot_cost))
        maximum_quantity = min(cash_lots, guide_lots) * board_lot
        if maximum_quantity == 0:
            block(
                "available_cash_below_board_lot",
                "提案 limit で最低一単元を買う cash が不足しています。",
            )
    quantity = maximum_quantity if selected_quantity is None else selected_quantity
    if selected_quantity is not None and selected_quantity > maximum_quantity:
        block(
            "selected_quantity_above_maximum",
            "選択数量が提案limit・確認済みcash・guideによる数量上限を超えています。",
        )
    projection_basis = reviewed and latest and confirmed_basis

    def projection_at(value: Decimal | None) -> dict[str, object] | None:
        if value is None or value <= 0 or price is None or price <= 0:
            return None
        return current_price_projection(
            assessment,
            price_yen=value,
            price_as_of=price_as_of,
            as_of=as_of,
            basis_confirmed=projection_basis,
            max_quote_age_days=market_price_max_age_days,
            price_basis="last_close_unadjusted" if quote is None else quote.price_basis,
        )

    current_projection = projection_at(price)
    limit_projection = projection_at(limit)
    return BuyTermsEvaluation(
        tuple(blockers),
        maximum,
        0 if blockers else quantity,
        maximum_quantity,
        None if maximum is None or price is None else price > maximum,
        current_projection,
        limit_projection,
    )
