"""新規購入工程で企業評価と現在の価格・保有・資本条件の誤判断を止める。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from baibai_engine.research.assessment import (
    StockAssessmentDocument,
)
from baibai_engine.research.assessment import (
    current_price_projection as assessment_price_projection,
)
from baibai_engine.research.thesis import ThesisDocument, current_price_projection
from baibai_engine.valuation.scenario import finite_decimal, maximum_entry_price

if TYPE_CHECKING:
    from baibai_engine.research.proposal import GateReason, QuoteInput


@dataclass(frozen=True, slots=True)
class EntryResult:
    eligible: bool
    reasons: tuple[str, ...]
    maximum_price_yen: Decimal | None
    quantity: int
    current_price_projection: dict[str, object] | None = None


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
    "required_return_below_current_policy",
    "positive_limit_required",
    "limit_above_pmax",
    "available_cash_below_board_lot",
]


@dataclass(frozen=True, slots=True)
class BuyTermsEvaluation:
    hard_blockers: tuple[GateReason, ...]
    maximum_price_yen: Decimal | None
    quantity: int
    current_price_above_maximum: bool | None
    current_price_projection: dict[str, object] | None
    limit_price_projection: dict[str, object] | None


def _positive_board_lot(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("positive board_lot and budget guide required")
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
    minimum_required_annual_return_pct: Decimal,
    market_price_max_age_days: int,
    held_quantity: int,
    active_reservation: bool,
    proposal_executed: bool,
    available_cash_yen: Decimal,
    budget_max_yen: Decimal,
    board_lot: int,
) -> BuyTermsEvaluation:
    """Apply hard gates to real observations and size separately at the author's limit."""
    from baibai_engine.research.proposal import GateReason

    if quote is not None and quote.ticker != assessment.input_snapshot.ticker:
        raise ValueError("quote and assessment ticker differ")
    limit = finite_decimal(proposed_limit_yen)
    cash, guide = finite_decimal(available_cash_yen), finite_decimal(budget_max_yen)
    board_lot = _positive_board_lot(board_lot)
    if guide <= 0:
        raise ValueError("positive board_lot and budget guide required")
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
    confirmed_basis = basis_confirmed and quote is not None and quote.basis_confirmed
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
        if valuation.required_annual_return_pct < finite_decimal(
            minimum_required_annual_return_pct
        ):
            block(
                "required_return_below_current_policy", "評価の要求率が現 policy floor 未満です。"
            )
        if limit > maximum:
            block("limit_above_pmax", "提案 limit が Base による購入上限を超えています。")
    quantity = 0
    if limit <= 0:
        block("positive_limit_required", "提案 limit は正の価格で指定してください。")
    else:
        lot_cost = limit * board_lot
        cash_lots = max(0, int(cash // lot_cost))
        guide_lots = max(1, int(guide // lot_cost))
        quantity = min(cash_lots, guide_lots) * board_lot
        if quantity == 0:
            block(
                "available_cash_below_board_lot",
                "提案 limit で最低一単元を買う cash が不足しています。",
            )
    projection_basis = reviewed and latest and confirmed_basis

    def projection_at(value: Decimal | None) -> dict[str, object] | None:
        if value is None or value <= 0 or price is None or price <= 0:
            return None
        return assessment_price_projection(
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
        None if maximum is None or price is None else price > maximum,
        current_projection,
        limit_projection,
    )


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
    # 将来cash・期間の起点を揃え、受取済み分配の再算入を防ぐ (#1242)。
    # docs/reference/thesis.md#planning-only-execution-pricing
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
