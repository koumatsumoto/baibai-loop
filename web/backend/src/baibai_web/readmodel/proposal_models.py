"""保存提案とexact評価pairの小さい表示契約。現在の注文資格は判定しない。"""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReport,
    ProposalCorrection,
)


class SavedAssessmentReviewView(BaseModel):
    reviewed_at: datetime
    primary_source_check: Literal["verified", "partially_verified", "unverified"]
    review_scope: str
    strongest_countercase: str
    uncertainty_acceptance_reason: str | None


class SavedAssessmentView(BaseModel):
    assessment_id: str
    core_sha256: str
    review_id: str
    as_of: date
    stance: Literal["supported", "uncertain", "unsupported"]
    investment_case_explanation: str
    investment_case_status: Literal["intact", "broken", "uncertain"]
    investment_case_status_reason: str
    invalidation_conditions: list[str]
    review: SavedAssessmentReviewView


class SavedQuoteView(BaseModel):
    price_yen: str
    price_as_of: date
    price_basis: Literal["last_close_unadjusted"]


class SavedReasonView(BaseModel):
    code: str
    message: str


class SavedLimitTermsView(BaseModel):
    limit_price_yen: str
    quantity: int
    notional_yen: str
    target_session: date
    valuation_as_of: date
    maximum_entry_price_yen: str | None
    conditions: list[str]
    warnings: list[str]


class UnfinishedResearchView(BaseModel):
    reason: str
    references: list[str]


class SavedRemainingRewardView(BaseModel):
    status: Literal["sufficient", "insufficient", "uncertain"]
    reason: str


class SavedLiquidityView(BaseModel):
    analysis_as_of: date | None
    adv_yen: str | None
    unavailable_reason: str | None


class SavedExposureRowView(BaseModel):
    key: str
    prospective_pct: str
    warning_pct: str


class SavedPortfolioExposureView(BaseModel):
    total_capital_yen: str | None
    ticker: SavedExposureRowView | None
    sector: SavedExposureRowView | None
    common_factors: list[SavedExposureRowView]
    common_factor_unclassified_tickers: list[str]


class _SavedProposalItemView(BaseModel):
    ticker: str
    rationale: str
    review_conditions: list[str]
    assessment: SavedAssessmentView | None
    saved_quote: SavedQuoteView | None
    liquidity: SavedLiquidityView | None
    unfinished: UnfinishedResearchView | None


class SavedBuyItemView(_SavedProposalItemView):
    action: Literal["buy"]
    timing: Literal["at_current_limit", "wait_limit", "place_limit"]
    terms: SavedLimitTermsView | None
    proposed_limit_yen: str | None
    terms_unavailable_reasons: list[SavedReasonView]


class _SavedHoldingItemView(_SavedProposalItemView):
    original_assessment: SavedAssessmentView | None
    original_investment_reason: str | None
    original_reason_unavailable_reason: str | None
    case_reassessment_reason: str
    remaining_reward: SavedRemainingRewardView | None


class SavedHoldItemView(_SavedHoldingItemView):
    action: Literal["hold"]
    holding_basis: Literal["investment_case_intact", "sufficient_remaining_reward"]
    return_calculation_unavailable_reason: str | None


class SavedSellItemView(_SavedHoldingItemView):
    action: Literal["sell"]
    reason_code: Literal["economic_invalidation", "insufficient_remaining_reward"]
    terms: SavedLimitTermsView | None
    terms_unavailable_reasons: list[SavedReasonView]


class SavedNoActionItemView(_SavedProposalItemView):
    action: Literal["no_action"]


class SavedDeferItemView(_SavedProposalItemView):
    action: Literal["defer"]
    blockers: list[SavedReasonView]
    resume_conditions: list[str]


type SavedProposalItemView = Annotated[
    SavedBuyItemView
    | SavedHoldItemView
    | SavedSellItemView
    | SavedNoActionItemView
    | SavedDeferItemView,
    Field(discriminator="action"),
]


class SavedProposalView(BaseModel):
    proposal_id: str
    as_of: date
    prepared_at: datetime
    published_at: datetime
    request_scope: Literal["discover_new", "specified_stocks", "review_positions"]
    headline: str
    comparison: str
    forgone: str
    warnings: list[str]
    items: list[SavedProposalItemView]
    portfolio_exposure: SavedPortfolioExposureView
    corrections: list[ProposalCorrection] = Field(default_factory=list)
    human_reports: list[HumanProposalReport] = Field(default_factory=list)


class LatestProposalView(BaseModel):
    generated_at: datetime
    status: Literal["ready", "empty", "unavailable"]
    error: str | None
    proposal: SavedProposalView | None
