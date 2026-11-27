// Generated from the Python models in web/backend/src/baibai_web/readmodel/.
// Run `uv run python -m baibai_web.contracts_export` after changing those models;
// `tools/quality/drift/check_readmodel_contract.py` refuses a stale copy.

export interface ConsensusEPS {
  provider_label: string
  fiscal_end_label: string | null
  eps_jpy_per_share: number | null
  analyst_count: number | null
  provider_published_at_utc: null
}

export interface DashboardView {
  generated_at: string
  ledger_exists: boolean
  ledger_error: string | null
  ledger_as_of: string | null
  /**
   * 市場評価の読取失敗。台帳事実は保持し、株価等の欠測だけの場合はnull。
   */
  valuation_error: string | null
  /**
   * 保有中の株数変更を照合できず、全売却報告から残高を確定できない。
   */
  holding_quantity_unresolved: boolean
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

export type EconomicBuyDecision = 'price_conditional_allocation'

export interface EstimatedModelChartView {
  selection_sha256: string
  input_known_at: string
  research_as_of: string
  generation_id: string | null
  rows: EstimatedPerspectiveView[]
  monex_fiscal_eps: MonexFiscalEPSView | null
}

export interface EstimatedPerspectiveView {
  model: 'company_financials' | 'company_growth' | 'market_context' | 'external_assessment' | 'technical_analysis' | 'industry_trends' | 'social_trends'
  assessment: 'supportive' | 'mixed' | 'adverse' | 'neutral' | 'context_only' | 'insufficient'
  summary: string
  evidence: string
  observed_on: string | null
  source_url: string | null
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

export type JsonValue = unknown

export interface LatestProposalView {
  generated_at: string
  status: 'ready' | 'empty' | 'unavailable'
  error: string | null
  proposal: SavedProposalView | null
}

export interface LatestUSProposalView {
  generated_at: string
  status: 'ready' | 'empty' | 'unavailable'
  error: string | null
  proposal: USProposalView | null
}

export interface MonexFiscalEPSView {
  source_url: string
  received_at_utc: string
  current_fy: ConsensusEPS | null
  next_fy: ConsensusEPS | null
  annualized_to_12_months: true | null
  share_basis: 'shares_at_provider_last_update' | null
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

export interface SavedAcquisitionLimitView {
  value_fact_id: string
  last_eligible_trade_date: string | null
}

export interface SavedAssessmentRefView {
  assessment_id: string
  core_sha256: string
  review_id: string
}

export interface SavedAssessmentRefreshView {
  prior_assessment_ref: SavedAssessmentRefView
  checked_at: string
  changed_sections: string[]
  material_information_check: string
  unchanged_assumptions_reason: string
  horizon_reason: string
  distribution_reason: string
  corporate_action_reason: string
}

export interface SavedAssessmentReviewView {
  reviewed_at: string
  primary_source_check: 'verified' | 'partially_verified' | 'unverified'
  review_scope: string
  strongest_countercase: string
  uncertainty_acceptance_reason: string | null
  checked_source_ids: string[] | null
  checked_sources: SavedAssessmentSourceView[] | null
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
  price_value_case: SavedPriceValueView | null
  investment_case_explanation: string
  investment_case_status: 'intact' | 'broken' | 'uncertain'
  investment_case_status_reason: string
  invalidation_conditions: string[]
  investment_case_source_ids: string[] | null
  investment_case_sources: SavedAssessmentSourceView[] | null
  hypothesis_checks: SavedHypothesisCheckView[] | null
  hypothesis_sources: SavedAssessmentSourceView[] | null
  refresh: SavedAssessmentRefreshView | null
  strongest_countercase: string | null
  realization: SavedValueRealizationView | null
  value_gap: SavedValueGapView | null
  estimated_model_chart: EstimatedModelChartView | null
  model_perspectives: SavedModelPerspectiveView[]
  model_perspective_sources: SavedAssessmentSourceView[]
  risk_analysis: SavedAssessmentRisksView | null
  valuation: SavedResolvedValuationView | SavedUnresolvedValuationView | null
  review: SavedAssessmentReviewView
}

export interface SavedBuyCapitalComparisonView {
  scope: 'available_cash_only'
  execution_assumption: 'full_fill_at_limit'
  return_basis: 'conditional_pretax_without_reinvestment'
  investor_taxes_and_fees_included: false
  cash_basis: 'constant_nominal_without_interest'
  horizon_months: number
  quantity_basis: 'author_selected'
  quantity: number
  limit_price_yen: string
  notional_yen: string
  unspent_cash_yen: string
  cash_only_terminal_value_yen: string
  base: SavedCapitalScenarioView
  downside: SavedCapitalScenarioView
  time_sensitivity: SavedBuyTimeSensitivityView | null
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
  economic_decision: EconomicBuyDecision | null
  saved_buy_comparison: SavedBuyCapitalComparisonView | null
}

export interface SavedBuyTimeSensitivityView {
  assumption: 'terminal_and_cumulative_cash_unchanged'
  additional_months: number
  horizon_months: number
  base: SavedScenarioTimeSensitivityView
  downside: SavedScenarioTimeSensitivityView
}

export interface SavedCapitalScenarioView {
  investment_total_value_yen: string
  profit_loss_yen: string
  capital_total_value_yen: string
}

export interface SavedCashView {
  available_cash_yen: string
  inputs_checked_at: string
  ledger_as_of: string
}

export interface SavedConditionalReturnView {
  total_value_per_share_yen: string
  total_return_pct: string
  annualized_return_pct: string
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
  saved_holding_origin: SavedHoldingOriginView | null
  saved_position: SavedHoldingPositionView | null
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
  saved_position: SavedHoldingPositionView | null
  original_assessment: SavedAssessmentView | null
  original_investment_reason: string | null
  original_reason_unavailable_reason: string | null
  case_reassessment_reason: string
  remaining_reward: SavedRemainingRewardView | null
  action: 'hold'
  holding_basis: 'investment_case_intact' | 'sufficient_remaining_reward'
  return_calculation_unavailable_reason: string | null
  saved_hold_quote_comparison: SavedHoldQuoteComparisonView | null
}

export interface SavedHoldQuoteComparisonView {
  price_context: 'saved_hold_quote'
  calculation_price_yen: string
  price_as_of: string
  price_basis: 'last_close_unadjusted'
  horizon_months: number
  return_basis: 'conditional_pretax_without_reinvestment'
  base: SavedConditionalReturnView
  downside: SavedConditionalReturnView
}

export interface SavedHoldingOriginView {
  assessment_ref: SavedAssessmentRefView | null
  assessment: SavedAssessmentView | null
  investment_reason: string | null
  reason_unavailable: string | null
}

export interface SavedHoldingPositionView {
  quantity: number
  quantity_basis_confirmed: boolean
  cost_yen: string
  inputs_checked_at: string
  ledger_as_of: string
}

export interface SavedHypothesisCheckView {
  statement: string
  origin: 'user' | 'research'
  result: 'supported' | 'refuted' | 'unresolved'
  reason: string
  source_ids: string[]
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

export interface SavedMarketContextView {
  source: 'proposal_fixed_selection'
  selection_sha256: string
  status: 'retained' | 'not_recorded' | 'unavailable'
  assessment: Record<string, JsonValue> | null
  reason: string | null
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

export interface SavedOpportunityFactView {
  fact_id: string
  value: string
  unit: string
  as_of: string
  source_ids: string[]
}

export interface SavedOpportunityQuoteView {
  fact_id: string
  price_yen: string
  as_of: string
  observed_at: string | null
  price_basis: string
  source_ids: string[]
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

export interface SavedPriceOnlyConditionView {
  explanation: string
  ceiling_yen: string | null
}

export interface SavedPriceValueView {
  status: 'supported_at_quote' | 'not_supported_at_quote' | 'undetermined'
  observed_quote: SavedOpportunityQuoteView | null
  price_compatible_conditions: string | null
  price_only_condition: SavedPriceOnlyConditionView | null
  acquisition_limit: SavedAcquisitionLimitView | null
  value_facts: SavedOpportunityFactView[]
  sources: SavedAssessmentSourceView[]
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
  saved_cash: SavedCashView | null
  saved_market_context: SavedMarketContextView | null
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

export interface SavedScenarioTimeSensitivityView {
  annualized_return_pct: string
  delayed_annualized_return_pct: string
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
  saved_position: SavedHoldingPositionView | null
  original_assessment: SavedAssessmentView | null
  original_investment_reason: string | null
  original_reason_unavailable_reason: string | null
  case_reassessment_reason: string
  remaining_reward: SavedRemainingRewardView | null
  action: 'sell'
  reason_code: 'economic_invalidation' | 'insufficient_remaining_reward' | 'consider_full_recovery' | 'consider_partial_recovery'
  terms: SavedLimitTermsView | null
  terms_unavailable_reasons: SavedReasonView[]
  saved_sell_limit_comparison: SavedSellLimitComparisonView | null
}

export interface SavedSellLimitComparisonView {
  price_context: 'author_sell_minimum'
  calculation_price_yen: string
  horizon_months: number
  return_basis: 'conditional_pretax_without_reinvestment'
  base: SavedConditionalReturnView
  downside: SavedConditionalReturnView
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
  source_ids: string[] | null
  sources: SavedAssessmentSourceView[] | null
}

export interface SavedValueGapView {
  explanation: string
  findings_source_ids: string[]
  findings_sources: SavedAssessmentSourceView[]
}

export interface SavedValueRealizationView {
  mechanism: string
  timing: 'known' | 'uncertain'
  timing_reason: string
  delay_risk: string
}

export type USProposalComparisonAmount = string

export interface USProposalComparisonScenarioView {
  terminal_value_usd_per_share: USProposalComparisonAmount
  eligible_distributions_usd_per_share: USProposalComparisonAmount
  difference_from_cash_usd_per_share: USProposalComparisonAmount
}

export interface USProposalComparisonView {
  cash_basis: 'same_usd_no_interest_tax_or_fees'
  base: USProposalComparisonScenarioView
  downside: USProposalComparisonScenarioView
}

export interface USProposalIexBandView {
  first_session_et: string
  last_session_et: string
  observations: number
  low_usd: string
  high_usd: string
  last_usd: string
}

export interface USProposalOfficialCloseView {
  price_usd: string
  origin: 'listing_exchange' | 'alpaca_sip_nasdaq_official_close'
  observed_at: string
}

export interface USProposalView {
  proposal_id: string
  draft_sha256: string
  ticker: string
  venue: 'XNAS' | 'XNYS'
  share_class: string
  currency: 'USD'
  session_et: string
  cutoff_at: string
  published_at: string
  reviewed_at: string
  action: 'defer' | 'no_action'
  reason: string
  revisit_when: string
  official_close: USProposalOfficialCloseView | null
  iex_band: USProposalIexBandView | null
  comparison_horizon_et: string | null
  comparison: USProposalComparisonView | null
  unresolved: string[]
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
