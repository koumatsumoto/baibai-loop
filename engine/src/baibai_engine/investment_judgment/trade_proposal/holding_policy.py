"""保有仮説の維持確認と、同日起点の残存見返り算術を分離する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    current_price_projection,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    GateReason,
    PositionInput,
    QuoteInput,
    RemainingReward,
)

type HoldingGateCode = Literal[
    "reviewed_assessment_required",
    "latest_assessment_required",
    "case_reassessment_required",
    "investment_case_uncertain",
    "primary_evidence_requires_verification",
    "remaining_reward_requires_confirmation",
    "valuation_unresolved",
    "valuation_basis_requires_refresh",
    "quote_missing_or_stale",
    "quantity_basis_unconfirmed",
    "corporate_action_unresolved",
]


@dataclass(frozen=True, slots=True)
class HoldingProposalEvaluation:
    action: Literal["hold", "sell", "defer"]
    reason_code: str
    hard_blockers: tuple[GateReason, ...]
    holding_basis: Literal["investment_case_intact", "sufficient_remaining_reward"] | None
    sell_quantity: int | None
    current_price_projection: dict[str, object] | None
    return_calculation_unavailable_reason: str | None


def evaluate_holding_proposal(
    assessment: StockAssessmentDocument,
    *,
    position: PositionInput,
    as_of: date,
    quote: QuoteInput | None,
    price_basis_confirmed: bool,
    remaining_reward: RemainingReward | None,
    case_reassessment_reason: str | None,
    primary_verified: bool,
    reviewed: bool,
    latest: bool,
    max_quote_age_days: int,
) -> HoldingProposalEvaluation:
    """Broken verified cases sell without quotes; intact holds need an explicit current check."""
    ticker = assessment.input_snapshot.ticker
    if position.ticker != ticker or (quote is not None and quote.ticker != ticker):
        raise ValueError("position, quote and assessment ticker differ")
    blockers: list[GateReason] = []

    def block(code: HoldingGateCode, message: str) -> None:
        blockers.append(GateReason(code=code, message=message))

    if not reviewed:
        block("reviewed_assessment_required", "独立 Review 済み評価が必要です。")
    if not latest:
        block("latest_assessment_required", "最新公開評価による再確認が必要です。")
    if not (case_reassessment_reason or "").strip():
        block("case_reassessment_required", "今回の仮説維持または反証確認理由が必要です。")
    if not primary_verified:
        block("primary_evidence_requires_verification", "material な一次根拠を確認してください。")
    case = assessment.investment_case
    if case.status == "uncertain":
        block("investment_case_uncertain", "仮説の維持または破綻が未確定です。")
    if blockers:
        return HoldingProposalEvaluation(
            "defer", blockers[0].code, tuple(blockers), None, None, None, None
        )
    if case.status == "broken":
        return HoldingProposalEvaluation(
            "sell",
            "economic_invalidation",
            (),
            None,
            position.quantity if position.quantity_basis_confirmed else None,
            None,
            None,
        )
    unavailable_code: HoldingGateCode | None = None
    if assessment.valuation.status != "resolved":
        unavailable_code = "valuation_unresolved"
    elif assessment.input_snapshot.as_of != as_of:
        unavailable_code = "valuation_basis_requires_refresh"
    elif quote is None or not 0 <= (as_of - quote.price_as_of).days <= max_quote_age_days:
        unavailable_code = "quote_missing_or_stale"
    elif not position.quantity_basis_confirmed:
        unavailable_code = "quantity_basis_unconfirmed"
    elif not price_basis_confirmed:
        unavailable_code = "corporate_action_unresolved"
    projection = current_price_projection(
        assessment,
        price_yen=None if quote is None else quote.price_yen,
        price_as_of=None if quote is None else quote.price_as_of,
        as_of=as_of,
        basis_confirmed=unavailable_code is None,
        max_quote_age_days=max_quote_age_days,
        price_basis="last_close_unadjusted" if quote is None else quote.price_basis,
    )
    if remaining_reward is None:
        return HoldingProposalEvaluation(
            "hold",
            "investment_case_intact",
            (),
            "investment_case_intact",
            None,
            projection,
            unavailable_code,
        )
    if remaining_reward.status == "uncertain":
        block("remaining_reward_requires_confirmation", remaining_reward.reason)
    elif unavailable_code is not None or projection is None:
        block(
            unavailable_code or "remaining_reward_requires_confirmation",
            "十分/不十分とする残存見返りには、同日起点 valuation と現在算術が必要です。",
        )
    if blockers:
        return HoldingProposalEvaluation(
            "defer", blockers[0].code, tuple(blockers), None, None, projection, unavailable_code
        )
    if remaining_reward.status == "sufficient":
        return HoldingProposalEvaluation(
            "hold",
            "sufficient_remaining_reward",
            (),
            "sufficient_remaining_reward",
            None,
            projection,
            None,
        )
    return HoldingProposalEvaluation(
        "sell", "insufficient_remaining_reward", (), None, position.quantity, projection, None
    )
