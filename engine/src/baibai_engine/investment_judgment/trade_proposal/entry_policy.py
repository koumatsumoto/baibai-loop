"""新規購入工程で銘柄評価と現在の価格・保有・資本条件の整合を確認する。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from baibai_engine.investment_judgment.stock_assessment.document import (
    CurrentPriceProjectionInputs,
    StockAssessmentDocument,
)
from baibai_engine.investment_judgment.stock_assessment.price_value import referenced_price_fact
from baibai_engine.investment_judgment.stock_assessment.scenario import (
    maximum_entry_price,
    project_buy_capital,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import GateReason, QuoteInput
from baibai_engine.primitives.numbers import finite_decimal

type BuyGateCode = Literal[
    "reviewed_assessment_required",
    "latest_assessment_required",
    "assessment_not_supported",
    "price_value_case_unrecorded",
    "price_value_undetermined",
    "price_value_not_supported",
    "limit_above_reviewed_price_condition",
    "valuation_basis_requires_refresh",
    "valuation_inputs_require_refresh",
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
    maximum_quantity: int | None
    current_price_above_maximum: bool | None
    current_price_projection: dict[str, object] | None
    limit_price_projection: dict[str, object] | None
    capital_comparison: dict[str, object] | None


def _positive_board_lot(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("positive board_lot and budget guide required")
    return value


def _selected_board_lot_quantity(value: object, board_lot: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value % board_lot:
        raise ValueError("selected_quantity must be a positive board-lot multiple")
    return value


def acquisition_blocker(
    assessment: StockAssessmentDocument, target_session: date | None
) -> GateReason | None:
    case = assessment.price_value_case
    eligibility = None if case is None else case.acquisition_limit
    if eligibility is None:
        return None
    deadline = eligibility.last_eligible_trade_date
    if target_session is None or deadline is None:
        return GateReason(
            code="right_acquisition_unconfirmed",
            message="取得期限のある権利が提案の対象立会で買い手に帰属するか未確認です。",
        )
    if target_session > deadline:
        return GateReason(
            code="right_acquisition_expired",
            message="提案の対象立会では、評価に含めた権利を新たに取得できません。",
        )
    return None


def prepare_buy_terms(
    assessment: StockAssessmentDocument,
    *,
    reviewed: bool,
    latest: bool,
    as_of: date,
    quote: QuoteInput | None,
    basis_confirmed: bool,
    market_price_max_age_days: int,
    held_quantity: int,
    active_reservation: bool,
    proposal_executed: bool,
    available_cash_yen: Decimal,
    budget_max_yen: Decimal,
    board_lot: int,
    target_session: date | None = None,
) -> Callable[[Decimal, int | None], BuyTermsEvaluation]:
    """同じ査読済み評価・実quote・資金に対し、指値だけを変えて比較できる。"""
    if quote is not None and quote.ticker != assessment.input_snapshot.ticker:
        raise ValueError("quote and assessment ticker differ")
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
    projection_inputs = CurrentPriceProjectionInputs(assessment)
    if input_errors := projection_inputs.derived_metric_errors:
        block(
            "valuation_inputs_require_refresh",
            "現在の価値適用には派生計算の入力を再確認してください: " + "; ".join(input_errors),
        )
    confirmed_basis = basis_confirmed and quote is not None
    if not confirmed_basis:
        block("corporate_action_unresolved", "株式単位と価格の basis を確認してください。")
    price = None if quote is None else finite_decimal(quote.price_yen)
    price_as_of = None if quote is None else quote.price_as_of
    if price_as_of is None or not 0 <= (as_of - price_as_of).days <= market_price_max_age_days:
        block("quote_missing_or_stale", "現在 quote が欠測、未来日、または鮮度期限外です。")
    if price is None or price <= 0:
        block("positive_quote_required", "正の実 quote が必要です。")
    # 一株の経済判断が未確認なら、現金で買える単元を提案の数量にしない。
    case = assessment.price_value_case
    condition_ceiling = None
    if case is None:
        block(
            "price_value_case_unrecorded",
            "元評価には価格に対する割安判断がありません。新たに評価・Reviewしてください。",
        )
    elif case.status == "undetermined":
        block("price_value_undetermined", "一株の価値と観測価格の比較は未確定です。")
    elif case.status == "not_supported_at_quote":
        condition_ceiling = (
            None if case.price_only_condition is None else case.price_only_condition.ceiling_yen
        )
        if condition_ceiling is None:
            block(
                "price_value_not_supported",
                "この価格で割安を支持せず、同じ権利で買える具体的な価格条件もありません。",
            )
    else:
        # 「100円で支持」の査読は、同じ日の120円での支持ではない。
        reference = referenced_price_fact(assessment)
        if reference is None or isinstance(reference.value, bool | str):
            block("price_value_undetermined", "割安判断が参照した一株の実価格を確認できません。")
        else:
            condition_ceiling = finite_decimal(reference.value)
    acquisition_issue = acquisition_blocker(assessment, target_session)
    if acquisition_issue is not None:
        blockers.append(acquisition_issue)
    valuation = assessment.valuation
    maximum = base_ceiling = None
    if (
        valuation.status != "resolved"
        or valuation.base is None
        or valuation.horizon_months is None
        or valuation.required_annual_return_pct is None
    ):
        block("valuation_unresolved", "Base、期間、要求率の確定が必要です。")
    elif not blockers:
        # 評価や株式の単位が失効したら元評価を残し、今の購入上限とは比較しない。
        base_ceiling = maximum_entry_price(
            valuation.base,
            horizon_months=valuation.horizon_months,
            required_annual_return_pct=valuation.required_annual_return_pct,
        )
        assert condition_ceiling is not None
        maximum = min(base_ceiling, condition_ceiling)
    prerequisites = tuple(blockers)
    blockers.clear()
    if held_quantity > 0:
        block("already_held", "既保有銘柄の新規買付は初期 policy の対象外です。")
    if active_reservation:
        block("active_reservation_exists", "既存の active 予約を確認してください。")
    if proposal_executed:
        block("proposal_already_executed", "この提案に対する買付は既に記録されています。")
    portfolio_blockers = tuple(blockers)
    project = (
        projection_inputs.for_quote(
            price_as_of=price_as_of,
            as_of=as_of,
            basis_confirmed=reviewed and latest and confirmed_basis and acquisition_issue is None,
            max_quote_age_days=market_price_max_age_days,
            price_basis=quote.price_basis,
        )
        if quote is not None and price is not None and price > 0
        else None
    )

    def projection_at(value: Decimal | None) -> dict[str, object] | None:
        return None if project is None or value is None or value <= 0 else project(value)

    current_projection = projection_at(price)

    def at_limit(proposed_limit_yen: Decimal, selected_quantity: int | None) -> BuyTermsEvaluation:
        limit = finite_decimal(proposed_limit_yen)
        if selected_quantity is not None:
            selected_quantity = _selected_board_lot_quantity(selected_quantity, board_lot)
        blockers = list(prerequisites)

        def block(code: BuyGateCode, message: str) -> None:
            blockers.append(GateReason(code=code, message=message))

        if maximum is not None:
            assert condition_ceiling is not None
            assert base_ceiling is not None
            if limit > condition_ceiling:
                block(
                    "limit_above_reviewed_price_condition",
                    "指値が独立Review済みの同一権利の価格条件を超えています。",
                )
            if limit > base_ceiling:
                block("limit_above_pmax", "提案 limit が Base による購入上限を超えています。")
        blockers.extend(portfolio_blockers)
        maximum_quantity = 0
        if limit <= 0:
            block("positive_limit_required", "提案 limit は正の価格で指定してください。")
        else:
            lot_cost = limit * board_lot
            cash_lots = max(0, int(cash // lot_cost))
            # guideは絶対上限ではないため、cashで賄える1単元まで除外しない。
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
        limit_projection = projection_at(limit)
        if limit_projection is not None:
            # 指値は計算条件。鮮度/basisを確かめた実quoteの観測日・終値へ置換しない。
            limit_projection = {
                key: value
                for key, value in limit_projection.items()
                if key not in {"price_yen", "price_as_of", "price_basis"}
            }
            limit_projection.update(
                price_context="buy_limit_ceiling",
                calculation_price_yen=str(limit),
                observed_quote=None if quote is None else quote.model_dump(mode="json"),
            )
        capital_comparison = None
        if not blockers:
            assert valuation.horizon_months is not None
            assert valuation.base is not None
            assert valuation.downside is not None
            capital_comparison = project_buy_capital(
                valuation.base,
                valuation.downside,
                horizon_months=valuation.horizon_months,
                limit=limit,
                cash=cash,
                quantity=quantity,
                author_selected=selected_quantity is not None,
            )
        return BuyTermsEvaluation(
            tuple(blockers),
            maximum,
            0 if blockers else quantity,
            # 現金から買える単元数を、保有・残注文・執行済みの提案で使える数量にしない。
            None
            if maximum is None
            or limit > maximum
            or held_quantity > 0
            or active_reservation
            or proposal_executed
            else maximum_quantity,
            None if maximum is None or price is None else price > maximum,
            deepcopy(current_projection),
            limit_projection,
            capital_comparison,
        )

    return at_limit
