"""保存提案とexact評価pairの小さい表示契約。現在の注文資格は判定しない。"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue

from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReport,
    ProposalCorrection,
)


class SavedModelPerspectiveView(BaseModel):
    model: Literal[
        "company_financials",
        "company_growth",
        "market_context",
        "external_assessment",
        "technical_analysis",
        "industry_trends",
        "social_trends",
    ]
    assessment: Literal[
        "supportive", "mixed", "adverse", "neutral", "insufficient", "not_applicable"
    ]
    summary: str
    source_ids: list[str]


class SavedAssessmentSourceView(BaseModel):
    source_id: str
    source_tier: Literal["primary", "secondary", "local_data"]
    ref: str | None
    provider: str | None
    dataset: str | None
    as_of: date
    retrieved_at: datetime
    used_for: str


class SavedPermanentLossRiskView(BaseModel):
    axis: Literal[
        "funding_liquidity",
        "debt_repayment",
        "cash_flow",
        "dilution",
        "customer_concentration",
        "structural_decline",
        "governance_accounting",
    ]
    assessment: Literal["acceptable", "adverse", "unknown"]
    evidence_status: Literal["verified", "partially_verified", "unverified"]
    summary: str
    as_of: date
    source_ids: list[str]


class SavedAssessmentRisksView(BaseModel):
    risks: list[SavedPermanentLossRiskView]
    unresolved_questions: list[str]
    sources: list[SavedAssessmentSourceView]


class SavedHypothesisCheckView(BaseModel):
    statement: str
    origin: Literal["user", "research"]
    result: Literal["supported", "refuted", "unresolved"]
    reason: str
    source_ids: list[str]


class SavedAssessmentReviewView(BaseModel):
    reviewed_at: datetime
    primary_source_check: Literal["verified", "partially_verified", "unverified"]
    review_scope: str
    strongest_countercase: str
    uncertainty_acceptance_reason: str | None
    checked_source_ids: list[str] | None = None
    checked_sources: list[SavedAssessmentSourceView] | None = None


class SavedValueGapView(BaseModel):
    explanation: str
    findings_source_ids: list[str]
    findings_sources: list[SavedAssessmentSourceView]


class SavedValueRealizationView(BaseModel):
    mechanism: str
    timing: Literal["known", "uncertain"]
    timing_reason: str
    delay_risk: str


class SavedValuationProjectionView(BaseModel):
    terminal_value_per_share_yen: str
    cash_distribution_per_share_yen: str
    annualized_return_pct: str
    calculation: str
    source_ids: list[str] | None = None
    sources: list[SavedAssessmentSourceView] | None = None


class SavedResolvedValuationView(BaseModel):
    status: Literal["resolved"] = "resolved"
    price_yen: str
    price_as_of: date
    price_observed_at: datetime | None
    price_basis: str | None
    horizon_months: int
    required_annual_return_pct: str
    base: SavedValuationProjectionView
    downside: SavedValuationProjectionView


class SavedUnresolvedValuationView(BaseModel):
    status: Literal["unresolved"] = "unresolved"
    reason: str


class SavedAssessmentRefView(BaseModel):
    assessment_id: str
    core_sha256: str
    review_id: str


class SavedAssessmentRefreshView(BaseModel):
    prior_assessment_ref: SavedAssessmentRefView
    checked_at: datetime
    changed_sections: list[str]
    material_information_check: str
    unchanged_assumptions_reason: str
    horizon_reason: str
    distribution_reason: str
    corporate_action_reason: str


class SavedOpportunityQuoteView(BaseModel):
    fact_id: str
    price_yen: Decimal
    as_of: date
    observed_at: datetime | None
    price_basis: str
    source_ids: list[str]


class SavedOpportunityFactView(BaseModel):
    fact_id: str
    value: str
    unit: str
    as_of: date
    source_ids: list[str]


class SavedPriceOnlyConditionView(BaseModel):
    explanation: str
    ceiling_yen: Decimal | None


class SavedAcquisitionLimitView(BaseModel):
    value_fact_id: str
    last_eligible_trade_date: date | None


class SavedPriceValueView(BaseModel):
    status: Literal["supported_at_quote", "not_supported_at_quote", "undetermined"]
    observed_quote: SavedOpportunityQuoteView | None
    price_compatible_conditions: str | None
    price_only_condition: SavedPriceOnlyConditionView | None
    acquisition_limit: SavedAcquisitionLimitView | None
    value_facts: list[SavedOpportunityFactView]
    sources: list[SavedAssessmentSourceView]


class SavedAssessmentView(BaseModel):
    assessment_id: str
    core_sha256: str
    review_id: str
    as_of: date
    stance: Literal["supported", "uncertain", "unsupported"]
    price_value_case: SavedPriceValueView | None = None
    investment_case_explanation: str
    investment_case_status: Literal["intact", "broken", "uncertain"]
    investment_case_status_reason: str
    invalidation_conditions: list[str]
    investment_case_source_ids: list[str] | None = None
    investment_case_sources: list[SavedAssessmentSourceView] | None = None
    hypothesis_checks: list[SavedHypothesisCheckView] | None = None
    hypothesis_sources: list[SavedAssessmentSourceView] | None = None
    refresh: SavedAssessmentRefreshView | None = None
    strongest_countercase: str | None = None
    realization: SavedValueRealizationView | None = None
    value_gap: SavedValueGapView | None = None
    model_perspectives: list[SavedModelPerspectiveView] = Field(default_factory=list)
    model_perspective_sources: list[SavedAssessmentSourceView] = Field(default_factory=list)
    risk_analysis: SavedAssessmentRisksView | None = None
    valuation: (
        Annotated[
            SavedResolvedValuationView | SavedUnresolvedValuationView,
            Field(discriminator="status"),
        ]
        | None
    ) = None
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


class SavedTurnoverWindowView(BaseModel):
    start_date: date
    end_date: date
    price_bar_count: int
    valid_turnover_count: int
    median_yen: str | None


class SavedLiquidityView(BaseModel):
    analysis_as_of: date | None
    price_window_end: date | None = None
    turnover_window: SavedTurnoverWindowView | None = None
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
    company_name: str | None = None
    sector: str | None = None
    rationale: str
    review_conditions: list[str]
    assessment: SavedAssessmentView | None
    saved_quote: SavedQuoteView | None
    liquidity: SavedLiquidityView | None
    unfinished: UnfinishedResearchView | None


class SavedCapitalScenarioView(BaseModel):
    investment_total_value_yen: str
    profit_loss_yen: str
    capital_total_value_yen: str


class SavedScenarioTimeSensitivityView(BaseModel):
    annualized_return_pct: str
    delayed_annualized_return_pct: str


class SavedBuyTimeSensitivityView(BaseModel):
    assumption: Literal["terminal_and_cumulative_cash_unchanged"]
    additional_months: int
    horizon_months: int
    base: SavedScenarioTimeSensitivityView
    downside: SavedScenarioTimeSensitivityView


class SavedBuyCapitalComparisonView(BaseModel):
    scope: Literal["available_cash_only"]
    execution_assumption: Literal["full_fill_at_limit"]
    return_basis: Literal["conditional_pretax_without_reinvestment"]
    investor_taxes_and_fees_included: Literal[False]
    cash_basis: Literal["constant_nominal_without_interest"]
    horizon_months: int
    quantity_basis: Literal["author_selected"]
    quantity: int
    limit_price_yen: str
    notional_yen: str
    unspent_cash_yen: str
    cash_only_terminal_value_yen: str
    base: SavedCapitalScenarioView
    downside: SavedCapitalScenarioView
    time_sensitivity: SavedBuyTimeSensitivityView | None = None


class SavedBuyItemView(_SavedProposalItemView):
    action: Literal["buy"]
    timing: Literal["at_current_limit", "wait_limit", "place_limit"]
    terms: SavedLimitTermsView | None
    proposed_limit_yen: str | None
    terms_unavailable_reasons: list[SavedReasonView]
    saved_buy_comparison: SavedBuyCapitalComparisonView | None = None


class SavedHoldingPositionView(BaseModel):
    quantity: int
    quantity_basis_confirmed: bool
    cost_yen: str
    inputs_checked_at: datetime
    ledger_as_of: datetime


class _SavedHoldingItemView(_SavedProposalItemView):
    saved_position: SavedHoldingPositionView | None = None
    original_assessment: SavedAssessmentView | None
    original_investment_reason: str | None
    original_reason_unavailable_reason: str | None
    case_reassessment_reason: str
    remaining_reward: SavedRemainingRewardView | None


class SavedConditionalReturnView(BaseModel):
    total_value_per_share_yen: str
    total_return_pct: str
    annualized_return_pct: str


class SavedHoldQuoteComparisonView(BaseModel):
    price_context: Literal["saved_hold_quote"] = "saved_hold_quote"
    calculation_price_yen: str
    price_as_of: date
    price_basis: Literal["last_close_unadjusted"]
    horizon_months: int
    return_basis: Literal["conditional_pretax_without_reinvestment"] = (
        "conditional_pretax_without_reinvestment"
    )
    base: SavedConditionalReturnView
    downside: SavedConditionalReturnView


class SavedHoldItemView(_SavedHoldingItemView):
    action: Literal["hold"]
    holding_basis: Literal["investment_case_intact", "sufficient_remaining_reward"]
    return_calculation_unavailable_reason: str | None
    saved_hold_quote_comparison: SavedHoldQuoteComparisonView | None = None


class SavedSellLimitComparisonView(BaseModel):
    price_context: Literal["author_sell_minimum"] = "author_sell_minimum"
    calculation_price_yen: str
    horizon_months: int
    return_basis: Literal["conditional_pretax_without_reinvestment"] = (
        "conditional_pretax_without_reinvestment"
    )
    base: SavedConditionalReturnView
    downside: SavedConditionalReturnView


class SavedSellItemView(_SavedHoldingItemView):
    action: Literal["sell"]
    reason_code: Literal[
        "economic_invalidation",
        "insufficient_remaining_reward",
        "consider_full_recovery",
        "consider_partial_recovery",
    ]
    terms: SavedLimitTermsView | None
    terms_unavailable_reasons: list[SavedReasonView]
    saved_sell_limit_comparison: SavedSellLimitComparisonView | None = None


class SavedNoActionItemView(_SavedProposalItemView):
    action: Literal["no_action"]


class SavedHoldingOriginView(BaseModel):
    assessment_ref: SavedAssessmentRefView | None
    assessment: SavedAssessmentView | None
    investment_reason: str | None
    reason_unavailable: str | None


class SavedDeferItemView(_SavedProposalItemView):
    action: Literal["defer"]
    saved_holding_origin: SavedHoldingOriginView | None = None
    saved_position: SavedHoldingPositionView | None = None
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


class SavedCashView(BaseModel):
    available_cash_yen: str
    inputs_checked_at: datetime
    ledger_as_of: datetime


class SavedMarketContextView(BaseModel):
    source: Literal["proposal_fixed_selection"]
    selection_sha256: str
    status: Literal["retained", "not_recorded", "unavailable"]
    assessment: dict[str, JsonValue] | None
    reason: str | None = None


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
    saved_cash: SavedCashView | None = None
    saved_market_context: SavedMarketContextView | None = None
    corrections: list[ProposalCorrection] = Field(default_factory=list)
    human_reports: list[HumanProposalReport] = Field(default_factory=list)


class LatestProposalView(BaseModel):
    generated_at: datetime
    status: Literal["ready", "empty", "unavailable"]
    error: str | None
    proposal: SavedProposalView | None


class ProposalHistoryView(BaseModel):
    generated_at: datetime
    status: Literal["ready", "empty", "partial", "unavailable"]
    error: str | None
    proposals: list[SavedProposalView]
    unavailable_proposal_ids: list[str]
