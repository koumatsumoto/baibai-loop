// Generated from the Python models in web/backend/src/baibai_web/readmodel/.
// Run `uv run python -m baibai_web.contracts_export` after changing those models;
// `tools/quality/drift/check_readmodel_contract.py` refuses a stale copy.

export interface DashboardView {
  generated_at: string
  ledger_exists: boolean
  ledger_error: string | null
  ledger_as_of: string | null
  /**
   * 市場評価の読取失敗。台帳事実は保持し、株価等の欠測だけの場合はnull。
   */
  valuation_error: string | null
  valuation_as_of: string | null
  total_capital_yen: number | null
  /**
   * 台帳上の未予約現金。売却代金は約定時にgrossで計上し、受渡済み資金や証券会社の発注可能額は表さない。
   */
  available_cash_yen: number | null
  reserved_cash_yen: number | null
  holdings_market_value_yen: number | null
  deployed_cost_yen: number | null
  realized_gross_pnl_yen: number | null
  cash_pct: number | null
  reserved_pct: number | null
  deployed_pct: number | null
  holdings: HoldingView[]
  reservations: ReservationView[]
  warnings: WarningView[]
}

export interface HoldingView {
  ticker: string
  company_name: string | null
  sector: string
  quantity: number
  deployed_cost_yen: number
  market_price_yen: string | null
  market_price_as_of: string | null
  market_value_yen: number | null
  unrealized_pnl_yen: number | null
  unrealized_pnl_pct: number | null
}

export interface HumanProposalReport {
  report_id: NonEmptyStr
  proposal_ref: ProposalReference
  outcome: 'not_purchased'
  statement: NonEmptyStr
  reported_at: string
  subject_at: string | null
  reason_quote: NonEmptyStr | null
  context_summary: NonEmptyStr | null
  recorded_at: string
}

export interface LatestProposalView {
  generated_at: string
  status: 'ready' | 'empty' | 'unavailable'
  error: string | null
  proposal: SavedProposalView | null
}

export type NonEmptyStr = string

export interface OutcomesView {
  error: string | null
  outcomes: PortfolioOutcomeView[]
}

export interface PortfolioOutcomeView {
  outcome_id: string
  horizon: string
  period_start_date: string
  period_end_date: string
  status: string
  reason: string | null
  /**
   * 記録済みの税・費用を反映した期間return。未報告の税・費用をゼロ確定とみなさない。
   */
  portfolio_twr_pct: number | null
  benchmark_cumulative_return_pct: number | null
}

export interface ProposalCorrection {
  correction_id: NonEmptyStr
  erroneous_ref: ProposalReference
  replacement_ref: ProposalReference
  reason: NonEmptyStr
  recorded_at: string
}

export interface ProposalHistoryView {
  generated_at: string
  status: 'ready' | 'empty' | 'partial' | 'unavailable'
  error: string | null
  proposals: SavedProposalView[]
  unavailable_proposal_ids: string[]
}

export interface ProposalReference {
  proposal_id: NonEmptyStr
  draft_sha256: SHA256
}

export interface ReservationView {
  reservation_id: string
  side: 'buy' | 'sell'
  ticker: string
  sector: string
  remaining_quantity: number
  price_guard_yen: string
  reserved_yen: number
  expires_at: string | null
  expiry_label: string | null
  route_label: string | null
}

export type SHA256 = string

export interface SavedAssessmentReviewView {
  reviewed_at: string
  primary_source_check: 'verified' | 'partially_verified' | 'unverified'
  review_scope: string
  strongest_countercase: string
  uncertainty_acceptance_reason: string | null
}

export interface SavedAssessmentRisksView {
  risks: SavedPermanentLossRiskView[]
  unresolved_questions: string[]
  sources: SavedAssessmentSourceView[]
}

export interface SavedAssessmentSourceView {
  source_id: string
  source_tier: 'primary' | 'secondary' | 'local_data'
  ref: string | null
  provider: string | null
  dataset: string | null
  as_of: string
  retrieved_at: string
  used_for: string
}

export interface SavedAssessmentView {
  assessment_id: string
  core_sha256: string
  review_id: string
  as_of: string
  stance: 'supported' | 'uncertain' | 'unsupported'
  investment_case_explanation: string
  investment_case_status: 'intact' | 'broken' | 'uncertain'
  investment_case_status_reason: string
  invalidation_conditions: string[]
  model_perspectives: SavedModelPerspectiveView[]
  model_perspective_sources: SavedAssessmentSourceView[]
  risk_analysis: SavedAssessmentRisksView | null
  valuation: SavedResolvedValuationView | SavedUnresolvedValuationView | null
  review: SavedAssessmentReviewView
}

export interface SavedBuyItemView {
  ticker: string
  company_name: string | null
  sector: string | null
  rationale: string
  review_conditions: string[]
  assessment: SavedAssessmentView | null
  saved_quote: SavedQuoteView | null
  liquidity: SavedLiquidityView | null
  unfinished: UnfinishedResearchView | null
  action: 'buy'
  timing: 'at_current_limit' | 'wait_limit' | 'place_limit'
  terms: SavedLimitTermsView | null
  proposed_limit_yen: string | null
  terms_unavailable_reasons: SavedReasonView[]
}

export interface SavedDeferItemView {
  ticker: string
  company_name: string | null
  sector: string | null
  rationale: string
  review_conditions: string[]
  assessment: SavedAssessmentView | null
  saved_quote: SavedQuoteView | null
  liquidity: SavedLiquidityView | null
  unfinished: UnfinishedResearchView | null
  action: 'defer'
  blockers: SavedReasonView[]
  resume_conditions: string[]
}

export interface SavedExposureRowView {
  key: string
  prospective_pct: string
  warning_pct: string
}

export interface SavedHoldItemView {
  ticker: string
  company_name: string | null
  sector: string | null
  rationale: string
  review_conditions: string[]
  assessment: SavedAssessmentView | null
  saved_quote: SavedQuoteView | null
  liquidity: SavedLiquidityView | null
  unfinished: UnfinishedResearchView | null
  original_assessment: SavedAssessmentView | null
  original_investment_reason: string | null
  original_reason_unavailable_reason: string | null
  case_reassessment_reason: string
  remaining_reward: SavedRemainingRewardView | null
  action: 'hold'
  holding_basis: 'investment_case_intact' | 'sufficient_remaining_reward'
  return_calculation_unavailable_reason: string | null
}

export interface SavedLimitTermsView {
  limit_price_yen: string
  quantity: number
  notional_yen: string
  target_session: string
  valuation_as_of: string
  maximum_entry_price_yen: string | null
  conditions: string[]
  warnings: string[]
}

export interface SavedLiquidityView {
  analysis_as_of: string | null
  price_window_end: string | null
  turnover_window: SavedTurnoverWindowView | null
  adv_yen: string | null
  unavailable_reason: string | null
}

export interface SavedModelPerspectiveView {
  model: 'company_financials' | 'company_growth' | 'market_context' | 'external_assessment' | 'technical_analysis' | 'industry_trends' | 'social_trends'
  assessment: 'supportive' | 'mixed' | 'adverse' | 'neutral' | 'insufficient' | 'not_applicable'
  summary: string
  source_ids: string[]
}

export interface SavedNoActionItemView {
  ticker: string
  company_name: string | null
  sector: string | null
  rationale: string
  review_conditions: string[]
  assessment: SavedAssessmentView | null
  saved_quote: SavedQuoteView | null
  liquidity: SavedLiquidityView | null
  unfinished: UnfinishedResearchView | null
  action: 'no_action'
}

export interface SavedPermanentLossRiskView {
  axis: 'funding_liquidity' | 'debt_repayment' | 'cash_flow' | 'dilution' | 'customer_concentration' | 'structural_decline' | 'governance_accounting'
  assessment: 'acceptable' | 'adverse' | 'unknown'
  evidence_status: 'verified' | 'partially_verified' | 'unverified'
  summary: string
  as_of: string
  source_ids: string[]
}

export interface SavedPortfolioExposureView {
  total_capital_yen: string | null
  ticker: SavedExposureRowView | null
  sector: SavedExposureRowView | null
  common_factors: SavedExposureRowView[]
  common_factor_unclassified_tickers: string[]
}

export type SavedProposalItemView = SavedBuyItemView | SavedHoldItemView | SavedSellItemView | SavedNoActionItemView | SavedDeferItemView

export interface SavedProposalView {
  proposal_id: string
  as_of: string
  prepared_at: string
  published_at: string
  request_scope: 'discover_new' | 'specified_stocks' | 'review_positions'
  headline: string
  comparison: string
  forgone: string
  warnings: string[]
  items: SavedProposalItemView[]
  portfolio_exposure: SavedPortfolioExposureView
  corrections: ProposalCorrection[]
  human_reports: HumanProposalReport[]
}

export interface SavedQuoteView {
  price_yen: string
  price_as_of: string
  price_basis: 'last_close_unadjusted'
}

export interface SavedReasonView {
  code: string
  message: string
}

export interface SavedRemainingRewardView {
  status: 'sufficient' | 'insufficient' | 'uncertain'
  reason: string
}

export interface SavedResolvedValuationView {
  status: 'resolved'
  price_yen: string
  price_as_of: string
  price_observed_at: string | null
  price_basis: string | null
  horizon_months: number
  required_annual_return_pct: string
  base: SavedValuationProjectionView
  downside: SavedValuationProjectionView
}

export interface SavedSellItemView {
  ticker: string
  company_name: string | null
  sector: string | null
  rationale: string
  review_conditions: string[]
  assessment: SavedAssessmentView | null
  saved_quote: SavedQuoteView | null
  liquidity: SavedLiquidityView | null
  unfinished: UnfinishedResearchView | null
  original_assessment: SavedAssessmentView | null
  original_investment_reason: string | null
  original_reason_unavailable_reason: string | null
  case_reassessment_reason: string
  remaining_reward: SavedRemainingRewardView | null
  action: 'sell'
  reason_code: 'economic_invalidation' | 'insufficient_remaining_reward'
  terms: SavedLimitTermsView | null
  terms_unavailable_reasons: SavedReasonView[]
}

export interface SavedTurnoverWindowView {
  start_date: string
  end_date: string
  price_bar_count: number
  valid_turnover_count: number
  median_yen: string | null
}

export interface SavedUnresolvedValuationView {
  status: 'unresolved'
  reason: string
}

export interface SavedValuationProjectionView {
  terminal_value_per_share_yen: string
  cash_distribution_per_share_yen: string
  annualized_return_pct: string
  calculation: string
}

export interface UnfinishedResearchView {
  reason: string
  references: string[]
}

export interface WarningView {
  code: string
  scope: string
  key: string
  actual_pct: number
  warning_pct: number
  overridden: boolean
}
