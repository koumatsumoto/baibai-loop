// Generated from web/backend/src/baibai_web/readmodel/models.py.
// Run `uv run python -m baibai_web.contracts_export` after changing those models;
// `tools/quality/drift/check_readmodel_contract.py` refuses a stale copy.

export interface AllocationAlternativeView {
  ticker: string
  disposition: string
  rationale: string
  thesis_id: string
  thesis_review_id: string | null
  case_status: string | null
  base_annualized_return_pct: number | null
  pmax_raw_yen: number | null
  valuation_as_of: string | null
  thesis_projection: Record<string, unknown>
  thesis_review: Record<string, unknown> | null
}

export interface AnalystRatingAssessment {
  distributor: string
  inputs: AnalystRatingHistory
  comparison: MetricComparison
}

export interface AnalystRatingHistory {
  latest: AnalystRatingObservation | null
  previous: AnalystRatingObservation | null
}

export interface AnalystRatingObservation {
  ticker: string
  fetched_at_utc: string
  snapshot_date: string
  distributor: string
  origin: string | null
  source_url: string
  parser_version: string
  contract_version: '1'
  rating: number | null
  rating_scale_min: number | null
  rating_scale_max: number | null
  rating_scale_direction: 'higher_is_positive' | 'lower_is_positive' | null
  rating_count: number | null
  rating_updated_at_utc: string | null
  rating_updated_label: string | null
}

export interface AssessmentClaimView {
  summary: string
  source_ids: string[]
}

export interface AssessmentContentView {
  known_at: string
  changes: AssessmentClaimView[]
  counter_evidence: AssessmentClaimView[]
  uncertainties: AssessmentClaimView[]
  implications: AssessmentClaimView[]
  inputs: Record<string, unknown>[]
}

/**
 * Existing L3 judgment fields shown before the current L2 readings.
 */
export interface AssessmentExcerptView {
  context_id: string
  as_of: string
  published_at: string
  age_days: number
  stale: boolean
  summary: string
  synthesis: HistoricalSynthesisView | null
  risk_environment: HistoricalRiskEnvironmentView | null
  scenarios: HistoricalScenarioView[]
  material_deltas: HistoricalMaterialDeltaView[]
  research_priority_hints: HistoricalResearchPriorityHintView[]
  bargain_topography: HistoricalFactSummaryView | null
  estimate_caveats: HistoricalEstimateCaveatView[]
  sizing_cautions: HistoricalSizingCautionView[]
  warnings: string[]
  assessment: AssessmentContentView | null
}

export interface AssessmentPublicationView {
  context_id: string
  as_of: string
  published_at: string
  summary: string
  age_days: number
  stale: boolean
  synthesis: HistoricalSynthesisView | null
  core: HistoricalCoreSectionView[]
  connection: HistoricalConnectionSectionView | null
  assessment: AssessmentContentView | null
  historical_payload: Record<string, unknown> | null
}

export interface AssessmentReviewView {
  attempt: number
  reviewer_identity: string
  reviewed_at: string
  conclusion: string
  open_findings: string[]
}

export interface AssessmentRevisionView {
  context_id: string
  as_of: string
  published_at: string
  summary: string
  age_days: number
  stale: boolean
}

export interface BenchmarkTrendView {
  as_of: string
  benchmark_ticker: string
  observation_date: string
  benchmark_return_20d: number | null
  benchmark_return_60d: number | null
  benchmark_gap_from_high: number | null
  trend_state: 'uptrend' | 'downtrend' | 'neutral' | 'unknown'
}

export interface CapitalAllocationAssessmentSummaryView {
  capital_allocation_assessment_id: string
  as_of: string
  published_at: string
  decision: string
  headline: string
  research_triage_id: string | null
  alternative_count: number
  allocated_ticker: string | null
  research_operation_id: string | null
}

export interface CapitalAllocationAssessmentView {
  capital_allocation_assessment_id: string
  as_of: string
  published_at: string
  decision: string
  headline: string
  research_triage_id: string | null
  market_context_assessment_id: string | null
  comparison: string
  foregone_alternatives: string
  research_operation_id: string | null
  selected_new_target_count: number | null
  unfinished_research: UnfinishedResearchView[]
  alternatives: AllocationAlternativeView[]
  content_review: AssessmentReviewView
}

export interface CapitalAllocationAssessmentsView {
  assessments: CapitalAllocationAssessmentSummaryView[]
}

/**
 * What changed between the latest machine run and the one before it.
 *
 * Every field is an observation or a comparison of observations. The view names no
 * cause and carries no recommendation: it tells the reader where to look, and the
 * decision to start a research cycle or a Position Review stays human.
 *
 * ``unavailable`` lists the sections no store could answer, so an empty section is
 * never read as "nothing changed". ``method_changed`` marks different screening or
 * Candidate Discovery method hashes, so no Review Set deltas are reported. Missing
 * method identity makes Review Set comparison unavailable. An E[r]-only model
 * change keeps membership deltas but makes
 * ``review_set_estimate`` unavailable and suppresses estimate movers.
 * Missing E[r] on either side of a shared ticker also marks that section unavailable,
 * while comparable tickers retain their movers.
 * Holdings that cannot be compared are counts rather than rows: repeating the same
 * list every day would bury the day's actual changes. ``er_moves_total`` says how
 * many names cleared the threshold before the row cap, so a capped list does not
 * hide its own remainder.
 */
export interface DailyDeltaView {
  generated_at: string
  as_of: string | null
  previous_as_of: string | null
  method_changed: boolean
  entered: ReviewSetEntryDeltaView[]
  exited: ReviewSetEntryDeltaView[]
  er_moves: ReviewSetExpectedReturnDeltaView[]
  er_moves_total: number
  holdings: HoldingDeltaView[]
  holdings_without_price: number
  unavailable: DeltaUnavailable[]
}

export interface DashboardView {
  generated_at: string
  ledger_exists: boolean
  ledger_error: string | null
  ledger_as_of: string | null
  ledger_stale: boolean
  valuation_as_of: string | null
  valuation_stale: boolean
  total_capital_yen: number | null
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
  research_load_errors: string[]
}

export type DeltaUnavailable = 'screening_run' | 'previous_screening_run' | 'review_set' | 'review_set_estimate' | 'holdings' | 'market'

export interface ErLevelCalibrationBandView {
  band_id: string
  quintile: number | null
  lower_er_annual: number | null
  upper_er_annual: number | null
  median_predicted_er_annual: number
  cohort_count: number
  median_n: number
  bases: ErLevelCalibrationBasisView[]
}

export interface ErLevelCalibrationBasisView {
  basis: string
  ticker_equal: ErLevelCalibrationStatsView
  cohort_equal: ErLevelCalibrationStatsView
}

export interface ErLevelCalibrationContextView {
  generated_at: string
  valid_through: string
  reference_horizon: string
  screening_rules_hash: string
  er_model_version: string
  primary_realized_basis: string
  secondary_realized_basis: string
  trap_basis: string
  horizons: ErLevelCalibrationHorizonView[]
}

export interface ErLevelCalibrationHorizonView {
  horizon: string
  as_of_start: string
  as_of_end: string
  cohort_count: number
  bands: ErLevelCalibrationBandView[]
}

export interface ErLevelCalibrationStatsView {
  median: number
  q25: number
  q10: number
  trap_rate: number
  n: number
}

export interface ExternalAssessment {
  ticker: string
  cutoff: string
  evaluated_at: string
  method_version: string
  model_valuations: ModelValuationAssessment[]
  analyst_ratings: AnalystRatingAssessment[]
  quants_scores: QuantsScoreAssessment[]
  tradingview: TradingViewAssessment
  origin_groups: OriginGroup[]
}

export interface ExternalAssessmentSecurityView {
  ticker: string
  name: string | null
  sector_33: string | null
  portfolio_state: PortfolioState
  has_research: boolean
}

export interface HistoricalConnectionSectionView {
  section_id: string
  series: HistoricalSeriesReferenceView[]
  core_section_ids: string[]
  fact_summary: HistoricalFactSummaryView[]
  judgment: HistoricalSectionJudgmentView
  research_priority_hints: HistoricalResearchPriorityHintView[]
  sector_tilts: HistoricalSectorTiltView[]
  sizing_cautions: HistoricalSizingCautionView[]
  bargain_topography: HistoricalFactSummaryView | null
  estimate_caveats: HistoricalEstimateCaveatView[]
}

export interface HistoricalCoreSectionView {
  section_id: string
  series: HistoricalSeriesReferenceView[]
  fact_summary: HistoricalFactSummaryView[]
  judgment: HistoricalSectionJudgmentView
  economic_connection: HistoricalEconomicConnectionView
  change_since_previous: string | null
  previous_scorecard_review: string | null
  material_deltas: HistoricalMaterialDeltaView[]
  risk_environment: HistoricalRiskEnvironmentView | null
  scenarios: HistoricalScenarioView[]
  monitoring_points: HistoricalMonitoringPointView[]
}

export interface HistoricalDominantForceView {
  force_id: string
  title: string
  summary: string
  transmission: string
  core_section_ids: string[]
  series: HistoricalSeriesReferenceView[]
  counter_evidence: string
  direction: string
  confidence: string
  source_ids: string[]
}

export interface HistoricalEconomicConnectionView {
  summary: string
  source_ids: string[]
}

export interface HistoricalEstimateCaveatView {
  summary: string
  applies_to: string
  affected_component: string
  materiality: string
  source_ids: string[]
}

export interface HistoricalFactSummaryView {
  summary: string
  source_ids: string[]
}

export interface HistoricalForceInteractionView {
  summary: string
  force_ids: string[]
  source_ids: string[]
}

export interface HistoricalMaterialDeltaView {
  channel: string
  direction: string
  materiality: string
  summary: string
  used_for: string
  source_ids: string[]
}

export interface HistoricalMonitoringPointView {
  event: string
  condition: string
  view_change: string
  summary: string
  source_ids: string[]
}

export interface HistoricalResearchPriorityHintView {
  summary: string
  applies_to: string
  source_ids: string[]
}

export interface HistoricalRiskEnvironmentView {
  stance: string
  confidence: string
  summary: string
  falsifiers: string[]
  source_ids: string[]
}

export interface HistoricalScenarioView {
  case: string
  direction: string
  probability: number | null
  summary: string
  conditions: string[]
  scorecard: HistoricalScorecardConditionView[]
  economic_implications: string[]
  source_ids: string[]
}

export interface HistoricalScorecardConditionView {
  series_id: string
  comparison: string
  threshold: number
  deadline: string
}

export interface HistoricalSectionJudgmentView {
  summary: string
  direction: string
  confidence: string
  source_ids: string[]
}

export interface HistoricalSectorTiltView {
  sector: string
  direction: string
  summary: string
  source_ids: string[]
}

export interface HistoricalSeriesReferenceView {
  series_id: string
  name: string
}

export interface HistoricalSizingCautionView {
  severity: string
  summary: string
  source_ids: string[]
}

/**
 * The integrated layer: named cross-channel forces and how they combine.
 */
export interface HistoricalSynthesisView {
  dominant_forces: HistoricalDominantForceView[]
  interactions: HistoricalForceInteractionView[]
}

/**
 * Observed holding price movement; holding decisions belong to Position Review.
 */
export interface HoldingDeltaView {
  ticker: string
  company_name: string | null
  change_since_previous_pct: number | null
  days_to_next_earnings: number | null
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
  pmax_raw_yen: number | null
  pmax_gap_pct: number | null
  latest_thesis_id: string | null
  disposition: string | null
  next_earnings_date: string | null
}

/**
 * The latest acquisition attempt for one series (not part of the reading itself).
 */
export interface IndicatorFetchHealthView {
  series_id: string
  status: string
  finished_at: string
  record_count: number
  error_message: string | null
}

export interface IndicatorGroupView {
  title: string
  series: IndicatorSeriesView[]
}

export interface IndicatorPointView {
  observed_at: string
  value: number
}

export interface IndicatorSeriesView {
  series_id: string
  label: string
  name: string
  unit: string
  tradingview_symbol: string | null
  points: IndicatorPointView[]
}

/**
 * Current L3 judgment first, followed by current L2 readings.
 */
export interface MarketContextView {
  market_snapshot: MarketSnapshotView | null
  latest_context: AssessmentExcerptView | null
  reading: ReadingView | null
  reports: AssessmentRevisionView[]
  groups: IndicatorGroupView[]
}

export interface MarketPointView {
  observation_date: string | null
  date: string
  benchmark_return_20d: number | null
  benchmark_return_60d: number | null
  breadth_pct_above_ma20: number | null
  breadth_sample_size: number
  benchmark_trend: 'uptrend' | 'downtrend' | 'neutral' | 'unknown'
}

export interface MarketSectorView {
  sector_33: string
  ticker_count: number
  median_return_20d: number
  median_return_60d: number | null
  pct_above_ma20: number | null
}

export interface MarketSnapshotView {
  as_of: string
  benchmark_ticker: string
  history_weeks: number
  master_snapshot_date: string | null
  points: MarketPointView[]
  sectors: MarketSectorView[]
  return_basis: 'price_return_excluding_cash_dividends'
}

export type MetaBatch = 'daily' | 'manual'

/**
 * Store freshness shown alongside every view and exported as views/meta.json.
 */
export interface MetaView {
  generated_at: string
  data_updated_at: string | null
  screening_as_of: string | null
  indicators_as_of: string | null
  app_db_updated_at: string | null
  batch: MetaBatch | null
}

export interface MetricComparison {
  metric: string
  state: 'not_observed' | 'first_observation' | 'missing' | 'recovered' | 'count_changed' | 'not_comparable' | 'same_value' | 'increased' | 'decreased'
  comparable: boolean
  reason: string | null
  latest_value: number | null
  previous_value: number | null
  latest_count: number | null
  previous_count: number | null
  delta: number | null
}

export interface ModelValuationAssessment {
  inputs: ModelValuationHistory
  comparisons: MetricComparison[]
}

export interface ModelValuationHistory {
  latest: ModelValuationObservation | null
  previous: ModelValuationObservation | null
}

export interface ModelValuationObservation {
  ticker: string
  fetched_at_utc: string
  snapshot_date: string
  distributor: string
  origin: string | null
  source_url: string
  parser_version: string
  contract_version: '1'
  consensus_target: number | null
  per_company_forecast: number | null
  per_consensus: number | null
  pbr: number | null
  currency: string | null
  unit: string | null
  share_basis: string | null
  consensus_target_origin: string | null
  consensus_target_period: string | null
  consensus_target_count: number | null
  consensus_target_updated_at_utc: string | null
  consensus_target_updated_label: string | null
  per_company_forecast_origin: string | null
  per_company_forecast_period: string | null
  per_company_forecast_count: number | null
  per_company_forecast_updated_at_utc: string | null
  per_company_forecast_updated_label: string | null
  per_consensus_origin: string | null
  per_consensus_period: string | null
  per_consensus_count: number | null
  per_consensus_updated_at_utc: string | null
  per_consensus_updated_label: string | null
  pbr_origin: string | null
  pbr_period: string | null
  pbr_count: number | null
  pbr_updated_at_utc: string | null
  pbr_updated_label: string | null
}

export interface OperationResearchTargetView {
  ticker: string
  purpose: 'new' | 'holding'
  outcome_status: 'reviewed' | 'proposal' | 'unfinished' | null
  thesis_id: string | null
  thesis_review_id: string | null
  unfinished_reason: string | null
  references: string[]
  checked_at: string | null
  holding_proposal: Record<string, unknown> | null
  holding_proposal_null_reason: string | null
}

export interface OperationResearchView {
  request: string
  research_triage_id: string | null
  targets: OperationResearchTargetView[]
  capital_allocation_assessment_id: string | null
  source_run_revision_id: string | null
  source_analysis_as_of: string | null
}

export interface OperationSessionView {
  operation_id: string
  session_kind: string
  status: string
  as_of: string
  ticker: string | null
  started_at: string
  completed_at: string | null
  payload: Record<string, unknown>
  research: OperationResearchView | null
}

export interface OperationsView {
  operations: OperationSessionView[]
  outcomes: PortfolioOutcomeView[]
}

export interface OriginGroup {
  origin: string
  references: OriginReference[]
}

export interface OriginReference {
  distributor: string
  kind: string
  metric: string
  fetched_at_utc: string
  source_url: string
}

export interface PortfolioOutcomeView {
  outcome_id: string
  horizon: string
  period_start_date: string
  period_end_date: string
  status: string
  reason: string | null
  portfolio_twr_pct: number | null
  benchmark_cumulative_return_pct: number | null
}

export type PortfolioState = 'unheld' | 'held' | 'reserved' | 'held_and_reserved'

export interface PositionReviewView {
  position_review_id: string
  as_of: string
  thesis_id: string
  action: string | null
  note: string | null
}

export interface QuantsScoreAssessment {
  inputs: QuantsScoreHistory
  comparisons: MetricComparison[]
}

export interface QuantsScoreHistory {
  latest: QuantsScoreObservation | null
  previous: QuantsScoreObservation | null
}

/**
 * Provider's quantitative scores; independent of human analyst consensus.
 */
export interface QuantsScoreObservation {
  ticker: string
  fetched_at_utc: string
  snapshot_date: string
  distributor: 'kabumap'
  origin: 'Quants Research'
  source_url: string
  parser_version: string
  contract_version: '1'
  score_as_of_date: string
  score_scale_max: 10
  composite_scale_max: 100
  growth_score: number | null
  undervaluation_score: number | null
  size_score: number | null
  technical_score: number | null
  financial_health_score: number | null
  market_trend_score: number | null
  composite_score: number | null
  industry_average_score: number | null
}

export interface ReadingSeriesView {
  read_semantics: StoreReadingSemanticsView | null
  series_id: string
  name: string
  category: string
  geography: string
  frequency: string
  unit: string
  latest_value: number | null
  observed_at: string | null
  staleness_days: number | null
  stale: boolean
  staleness_warn_days: number
  next_print_estimate: string | null
  print_due_in_days: number | null
  window_years: number
  window_observations: number
  expected_observations: number | null
  insufficient_history: boolean
  statistic: string
  statistic_unit: string
  statistic_value: number | null
  percentile: number | null
  z_score: number | null
  short_trend: ReadingTrendView | null
  long_trend: ReadingTrendView | null
  flags: string[]
}

export interface ReadingTrendView {
  months: number
  anchor_observed_at: string
  anchor_value: number
  change: number
  direction: string
}

/**
 * The machine reading of every registered series for one as-of date.
 *
 * A projection of the L2 reading, recomputed from the indicator store; the panel it
 * feeds is a display of that reading and not a second canonical artifact.
 * ``fetch_health`` rides along because staleness alone cannot see a provider that has
 * just gone silent — a low-frequency series stays inside its threshold for weeks.
 */
export interface ReadingView {
  read_semantics: StoreReadingSemanticsView | null
  as_of: string
  rules_revision: string
  series: ReadingSeriesView[]
  fetch_health: IndicatorFetchHealthView[]
}

export interface ResearchRevisionView {
  as_of: string
  thesis_id: string
  disposition: string
  pmax_raw_yen: number | null
  review_id: string | null
  status: string
}

export interface ResearchTriageDetailView {
  research_triage_id: string
  review_set_id: string
  run_revision_id: string
  as_of: string
  published_at: string
  entries: ResearchTriageEntryView[]
  payload: Record<string, unknown>
}

export interface ResearchTriageEntryView {
  ticker: string
  decision: string
  priority: number | null
  rationale: string
  research_question: string | null
  key_risk: string | null
  review_set_entry_snapshot: ReviewSetEntrySnapshotView | null
}

export interface ResearchTriageView {
  research_triage_id: string
  review_set_id: string
  run_revision_id: string
  as_of: string
  published_at: string
  entries: ResearchTriageEntryView[]
}

export interface ReservationView {
  reservation_id: string
  side: 'buy' | 'sell'
  ticker: string
  sector: string
  remaining_quantity: number
  price_guard_yen: string
  reserved_yen: number
  expires_at: string
  expiry_label: string | null
  route_label: string | null
}

export interface ReviewSetAnalysisView {
  identity_liquidity: ReviewSetIdentityLiquidityView
  valuation: ReviewSetValuationView
  current_earnings: ReviewSetCurrentEarningsView
  normalized_earnings: ReviewSetNormalizedEarningsView
  asset_value: ReviewSetAssetValueView
  reinvestment: ReviewSetReinvestmentView | null
  expected_return: ReviewSetExpectedReturnView
  data_quality: ReviewSetDataQualityView
  context: ReviewSetContextView
}

export interface ReviewSetAssetValueView {
  asset_backed_ratio: number | null
  net_cash_to_market_cap: number | null
  investment_securities: number | null
  equity_ratio: number | null
}

export interface ReviewSetContextView {
  next_earnings_status: string | null
  next_earnings_estimated_date: string | null
  margin_short_to_adv: number | null
  tse_capital_policy_status: string | null
  large_holding_filing_within_lookback: boolean | null
  tender_offer_filing_within_lookback: boolean | null
}

export interface ReviewSetCurrentEarningsView {
  fcf_yield: number | null
  ocf_yield: number | null
  forecast_special_gain_flag: boolean | null
  forecast_full_year_loss_flag: boolean | null
}

export interface ReviewSetDataQualityView {
  bs_carry_forward_fields: string | null
  bs_carry_forward_lag_days: number | null
  edinet_failure_reasons: string | null
  stale_fin_flag: boolean | null
  ttm_quality_ev_ebitda: 'exact' | 'approximated' | 'unavailable' | null
  ttm_quality_fcf: 'exact' | 'approximated' | 'unavailable' | null
}

/**
 * A ticker whose presence in the Review Set changed between two runs.
 *
 * ``disclosed_since_previous`` is ``null`` when the store that holds disclosure
 * dates could not answer, which must not read as "no disclosure".
 */
export interface ReviewSetEntryDeltaView {
  ticker: string
  company_name: string | null
  er_annual_pct: number | null
  disclosed_since_previous: boolean | null
}

/**
 * Machine coordinates frozen into one Research Triage judgment entry.
 */
export interface ReviewSetEntrySnapshotView {
  name: string | null
  sector_33: string | null
  nominations: Record<string, unknown>[] | null
  expected_return: Record<string, unknown> | null
  data_quality: Record<string, unknown> | null
}

/**
 * One member of the exact approach Nomination union.
 */
export interface ReviewSetEntryView {
  ticker: string
  name: string | null
  sector_33: string | null
  nominations: ReviewSetNominationView[]
  analysis: ReviewSetAnalysisView
}

/**
 * A ticker in both Review Sets whose machine E[r] moved most.
 */
export interface ReviewSetExpectedReturnDeltaView {
  ticker: string
  company_name: string | null
  er_annual_pct: number | null
  previous_er_annual_pct: number | null
  change_pp: number
}

export interface ReviewSetExpectedReturnView {
  er_annual: number | null
  er_reversion_annual: number | null
  er_carry_annual: number | null
  fv_sector_median_yen: number | null
  fv_self_range_yen: number | null
  er_origin: string | null
  er_model_version: string | null
  er_unit: string | null
  er_assumptions: string | null
  dividend_yield: number | null
  dividend_basis: string | null
  net_share_change_yoy: number | null
  carry_missing_assumption: string | null
}

export interface ReviewSetIdentityLiquidityView {
  market_cap_oku: number | null
  avg_turnover_oku: number | null
  listing_span_days: number | null
  jpx_flags: string[] | null
}

export interface ReviewSetNominationView {
  valuation_approach_id: string
  valuation_method_id: string
  rank: number
}

export interface ReviewSetNormalizedEarningsView {
  normalized_per_3fy: number | null
  normalized_per_3fy_sector_gap: number | null
}

export interface ReviewSetReinvestmentView {
  p_s_sector_gap: number | null
  operating_return_on_capital_proxy: number | null
  sales_yoy: number | null
  operating_margin: number | null
  fcf_yield: number | null
}

export interface ReviewSetValuationView {
  per_forward: number | null
  per_trailing: number | null
  pbr: number | null
  ev_ebitda: number | null
  p_s: number | null
  pcfr: number | null
}

export interface ReviewSetView {
  review_set_id: string
  run_revision_id: string
  created_at: string
  entries: ReviewSetEntryView[]
}

/**
 * Stable UI projection of one retained screening run.
 */
export interface ScreeningHistoryRunView {
  run: ScreeningRunView
  rows: SecurityAnalysisRowView[]
}

export interface ScreeningHistoryView {
  dates: string[]
}

export interface ScreeningRunView {
  run_id: string
  as_of: string
  generated_at: string
  universe_size: number
  analyzed_security_count: number
  run_revision_id: string
  stale: boolean
}

export interface ScreeningView {
  run: ScreeningRunView | null
  security_analyses: SecurityAnalysisRowView[]
  external_assessment_securities: ExternalAssessmentSecurityView[]
  external_assessment_cutoff: string | null
  review_sets: ReviewSetView[]
  research_triages: ResearchTriageView[]
  capital_allocation_assessments: CapitalAllocationAssessmentSummaryView[]
  er_level_calibration: ErLevelCalibrationContextView | null
}

export interface SecurityAnalysisRowView {
  ticker: string
  name: string | null
  sector_33: string | null
  market_cap_oku: number | null
  avg_turnover_oku: number | null
  per_trailing: number | null
  normalized_per_3fy: number | null
  per_forward: number | null
  pbr: number | null
  ev_ebitda: number | null
  p_s: number | null
  pcfr: number | null
  dividend_yield: number | null
  dividend_basis: string | null
  dividend_split_factor: number | null
  er_annual: number | null
  er_reversion_annual: number | null
  er_carry_annual: number | null
  net_share_change_yoy: number | null
  carry_missing_assumption: string | null
  er_model_version: string | null
  er_unit: string | null
  er_anchor_metrics: string | null
  er_assumptions: string | null
  net_cash_to_market_cap: number | null
  fcf_yield: number | null
  ocf_yield: number | null
  equity_ratio: number | null
  sales_yoy: number | null
  operating_profit_yoy: number | null
  sector_relative_strength_percentile: number | null
  price_change_20d: number | null
  gap_from_52w_low: number | null
  next_earnings_date: string | null
  margin_week_end: string | null
  margin_long_to_adv: number | null
  margin_short_to_adv: number | null
  margin_long_share: number | null
  margin_long_delta_26w: number | null
  margin_std_long_share: number | null
  data_quality_flags: string[]
  portfolio_state: PortfolioState
  has_research: boolean
  fair_value_anchor_yen: number | null
  fair_value_gap_pct: number | null
  er_level_quintile: number | null
  er_meets_8_5pct_band: boolean
}

export interface SecurityDetailView {
  market_context: SecurityMarketContextView | null
  external_assessment: ExternalAssessment | null
  ticker: string
  company_name: string | null
  sector: string | null
  holding: HoldingView | null
  revisions: ResearchRevisionView[]
  latest_thesis: ThesisDetailView | null
  position_reviews: PositionReviewView[]
  security_analysis: SecurityAnalysisRowView | null
  screening_run: ScreeningRunView | null
}

export interface SecurityMarketContextView {
  as_of: string
  master: SecurityMarketMasterView | null
  price: SecurityPriceContextView | null
  relative: SecurityRelativeContextView | null
  benchmark_trend: BenchmarkTrendView | null
  return_basis: 'price_return_excluding_cash_dividends'
}

export interface SecurityMarketMasterView {
  name: string
  market_segment: string
  sector_33: string
  is_common_stock: boolean
  snapshot_date: string
}

export interface SecurityPeerContextView {
  sector_33: string
  peer_count: number
  peer_median_return_20d: number
  relative_return_20d: number | null
  comparison_start_date: string
  comparison_end_date: string
  master_snapshot_date: string | null
}

export interface SecurityPriceContextView {
  resolved_date: string
  close: number
  price_basis: 'adjusted_close_preferred'
  return_1d: number | null
  return_5d: number | null
  return_20d: number | null
  return_60d: number | null
  high_52w: number
  low_52w: number
  gap_from_52w_low: number | null
  gap_from_52w_high: number | null
  self_range_percentile: number
  self_range_window_bars: number
  realized_vol_20d: number | null
  avg_turnover_20d_oku: number | null
  bar_count: number
}

export interface SecurityRelativeContextView {
  benchmark_ticker: string
  relative_1d: number | null
  relative_5d: number | null
  relative_20d: number | null
  relative_60d: number | null
  sector: SecurityPeerContextView | null
}

/**
 * The store reader's selection contract, not a guarantee for arbitrary pure readers.
 */
export interface StoreReadingSemanticsView {
  mode: 'recomputed_from_current_store'
  point_in_time_guaranteed: false
  known_at_applied: false
  limitation: string
  vintage_selection: 'publication_vintage_cutoff' | 'latest_stored_history' | null
  vintage_cutoff: string | null
}

export interface TaskView {
  task_id: string
  title: string
  kind: string
  status: string
  ticker: string | null
  due_date: string
  event_label: string | null
  event_date: string | null
  overdue: boolean
}

/**
 * Task/event workflow projection, separate from portfolio presentation.
 */
export interface TasksView {
  generated_at: string
  tasks_exist: boolean
  open_tasks: TaskView[]
  next_task: TaskView | null
  upcoming_events: UpcomingEventView[]
  ledger_error: string | null
}

export interface ThesisDetailView {
  revision: ResearchRevisionView
  projection: Record<string, unknown>
}

export interface TradingViewAssessment {
  inputs: TradingViewHistory
  availability: 'not_observed' | 'unresolved' | 'all_null' | 'forecast_fields_present'
  forecast_fields: string[]
  comparisons: MetricComparison[]
}

export interface TradingViewHistory {
  latest: TradingViewObservation | null
  previous: TradingViewObservation | null
}

export interface TradingViewObservation {
  snapshot_date: string
  ticker: string
  provider_symbol: string
  fetched_at_utc: string
  fetch_status: 'ok' | 'unresolved'
  quote_at_utc: string | null
  estimate_currency: string | null
  estimate_unit: string | null
  provider_updated_at_utc: string | null
  target_period_label: string | null
  target_period_end: string | null
  analyst_rating: string | null
  recommendation_total: number | null
  price_target_average: number | null
  price_target_high: number | null
  price_target_low: number | null
  eps_forecast_next_fq: number | null
  eps_forecast_next_fy: number | null
  revenue_forecast_next_fq: number | null
  revenue_forecast_next_fy: number | null
  forward_pe: number | null
  forward_ps: number | null
  forward_ev_ebitda: number | null
  ebitda_estimate_fy: number | null
  free_cash_flow_estimate_fy: number | null
  capex_estimate_fy: number | null
  total_debt_estimate_fy: number | null
  quote_close: number | null
  quote_currency: string | null
}

export interface UnfinishedResearchView {
  ticker: string
  reason: string
  references: string[]
}

export interface UpcomingEventView {
  event_date: string
  kind: 'earnings' | 'reservation_expiry'
  ticker: string | null
  label: string
  days_until: number
}

export interface WarningView {
  code: string
  scope: string
  key: string
  actual_pct: number
  warning_pct: number
  overridden: boolean
}
