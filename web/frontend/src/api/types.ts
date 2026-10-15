// Generated from web/backend/src/baibai_web/readmodel/models.py.
// Run `uv run python -m baibai_web.contracts_export` after changing those models;
// `tools/quality/drift/check_readmodel_contract.py` refuses a stale copy.

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

export interface AssessmentRefView {
  assessment_id: string
  core_sha256: string
  review_id: string
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

export interface CandidateDiscoveryMethodIdentityView {
  method_id: string
  method_hash: string
  nomination_depth: number
}

export interface CandidateDiscoverySummaryView {
  method: CandidateDiscoveryMethodIdentityView
  common_eligible_count: number
  candidate_counts_by_approach: Record<string, number>
  candidate_count: number
}

/**
 * A ticker whose presence in the 候補 changed between two runs.
 *
 * ``disclosed_since_previous`` is ``null`` when the store that holds disclosure
 * dates could not answer, which must not read as "no disclosure".
 */
export interface CandidateEntryDeltaView {
  ticker: string
  company_name: string | null
  er_annual_pct: number | null
  disclosed_since_previous: boolean | null
}

/**
 * A ticker in both 候補s whose machine E[r] moved most.
 */
export interface CandidateExpectedReturnDeltaView {
  ticker: string
  company_name: string | null
  er_annual_pct: number | null
  previous_er_annual_pct: number | null
  change_pp: number
}

export interface CandidateReasonView {
  valuation_approach_id: 'current-earnings-power' | 'normalized-earnings-power' | 'asset-value' | 'reinvestment-value'
  valuation_method_id: string
  rank: number
}

/**
 * What changed between the latest machine run and the one before it.
 *
 * The view compares observations, derived metrics, and machine estimates without
 * turning them into causal claims or recommendations. It shows where to look; the
 * decision to start new-stock or holding research stays human.
 *
 * ``unavailable`` lists the sections no store could answer, so an empty section is
 * never read as "nothing changed". ``method_changed`` marks different screening or
 * Candidate Discovery method hashes, so no 候補 deltas are reported. Missing
 * method identity makes 候補 comparison unavailable. An E[r]-only model
 * change keeps membership deltas but makes
 * ``candidate_estimate`` unavailable and suppresses estimate movers.
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
  entered: CandidateEntryDeltaView[]
  exited: CandidateEntryDeltaView[]
  er_moves: CandidateExpectedReturnDeltaView[]
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

export type DeltaUnavailable = 'screening_run' | 'previous_screening_run' | 'candidates' | 'candidate_estimate' | 'holdings' | 'market'

export interface ErLevelCalibrationBandView {
  band_id: string
  quintile: number | null
  lower_er_annual: number | null
  upper_er_annual: number | null
  median_predicted_er_annual: number | null
  cohort_count: number
  median_n: number
  bases: ErLevelCalibrationBasisView[]
}

export interface ErLevelCalibrationBasisView {
  basis: string
  coverage: ErLevelCalibrationCoverageView
  observed_cohort_count: number
  median_n: number
  ticker_equal: ErLevelCalibrationStatsView | null
  cohort_equal: ErLevelCalibrationStatsView | null
}

export interface ErLevelCalibrationContextView {
  interpretation: string
  required_asofs: string[]
  evidence_complete: boolean
  evidence_blocking_reasons: string[]
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

export interface ErLevelCalibrationCoverageView {
  declared_n: number
  observed_n: number
  missing_n: number
  missing_reason_counts: Record<string, number>
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

export interface GateReasonView {
  code: string
  message: string
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
 * Observed holding price movement; holding decisions belong to Investment Proposal.
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
  pmax_gap_unavailable_reason: string | null
  latest_assessment_id: string | null
  stance: string | null
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

export interface InvestmentProposalItemView {
  ticker: string
  action: 'buy' | 'hold' | 'sell' | 'no_action' | 'defer'
  rationale: string
  review_conditions: string[]
  assessment_ref: AssessmentRefView | null
  timing: string | null
  proposed_limit_yen: string | null
  terms: LimitTermsView | null
  terms_unavailable_reasons: GateReasonView[]
  order_readiness: 'ready' | 'needs_terms' | 'not_applicable'
  position: Record<string, unknown> | null
  holding_basis: string | null
  case_reassessment_reason: string | null
  remaining_reward: Record<string, unknown> | null
  return_calculation_unavailable_reason: string | null
  reason_code: string | null
  blockers: GateReasonView[]
  resume_conditions: string[]
  assessment_projection: Record<string, unknown> | null
  assessment_review: Record<string, unknown> | null
}

/**
 * 取得時刻付きの現在再試算。保存Proposal/termsとは独立したread-only結果。
 */
export interface InvestmentProposalPreviewView {
  saved_proposal_id: string
  previewed_at: string
  status: 'unchanged' | 'changed' | 'blocked'
  current_inputs: Record<string, unknown> | null
  items: Record<string, unknown>[]
  change_reasons: GateReasonView[]
  warnings: string[]
  portfolio_exposure: Record<string, unknown> | null
}

export interface InvestmentProposalSummaryView {
  proposal_id: string
  as_of: string
  published_at: string
  request_scope: string
  headline: string
  item_count: number
  tickers: string[]
  actions: string[]
  research_operation_id: string
}

export interface InvestmentProposalView {
  proposal_id: string
  as_of: string
  prepared_at: string
  published_at: string
  request_scope: string
  headline: string
  comparison: string
  forgone: string
  market_context_assessment_id: string | null
  research_scope: Record<string, unknown>
  saved_inputs: Record<string, unknown>
  items: InvestmentProposalItemView[]
  review: Record<string, unknown>
  draft_sha256: string
  liquidity_context: Record<string, unknown>[]
  warnings: string[]
  portfolio_exposure: Record<string, unknown>
  unfinished_research: UnfinishedResearchView[]
}

export interface InvestmentProposalsView {
  proposals: InvestmentProposalSummaryView[]
  next_cursor: string | null
}

/**
 * 保存当時の具体条件。現在条件、注文受理、約定を推定しない。
 */
export interface LimitTermsView {
  side: 'buy' | 'sell'
  limit_price_yen: string
  quantity: number
  notional_yen: string
  target_session: string
  expires_at: string
  valuation_as_of: string
  maximum_entry_price_yen: string | null
  price_grid_rule_id: string
  conditions: string[]
  warnings: string[]
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
  selection_rationale: string | null
  research_question: string | null
  key_risk: string | null
  ticker: string
  purpose: 'new' | 'holding'
  outcome_status: 'reviewed' | 'unfinished' | null
  assessment_ref: AssessmentRefView | null
  unfinished_reason: string | null
  references: string[]
}

export interface OperationResearchView {
  request_scope: string | null
  selection_summary: string | null
  max_deep_research_targets: number | null
  request: string
  targets: OperationResearchTargetView[]
  proposal_id: string | null
  proposal_draft_sha256: string | null
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
  /**
   * 記録済みの税・費用を反映した期間return。未報告の税・費用をゼロ確定とみなさない。
   */
  portfolio_twr_pct: number | null
  benchmark_cumulative_return_pct: number | null
}

export type PortfolioState = 'unheld' | 'held' | 'reserved' | 'held_and_reserved'

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
  ticker: string
  company_name: string
  sector: string
  summary: string
  as_of: string
  published_at: string
  assessment_id: string
  stance: string
  pmax_original_yen: number | null
  review_id: string
  status: string
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

/**
 * Stable UI projection of one retained screening run.
 */
export interface ScreeningHistoryRunView {
  run: ScreeningRunView
  rows: StockAnalysisRowView[]
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
  stock_analyses: StockAnalysisRowView[]
  external_assessment_securities: ExternalAssessmentSecurityView[]
  external_assessment_cutoff: string | null
  candidate_discovery: CandidateDiscoverySummaryView | null
  investment_proposals: InvestmentProposalSummaryView[]
  er_level_calibration: ErLevelCalibrationContextView | null
}

export interface SecurityDetailView {
  market_context: SecurityMarketContextView | null
  external_assessment: ExternalAssessment | null
  ticker: string
  company_name: string | null
  sector: string | null
  holding: HoldingView | null
  revisions: ResearchRevisionView[]
  latest_assessment: StockAssessmentDetailView | null
  investment_proposals: InvestmentProposalSummaryView[]
  stock_analysis: StockAnalysisRowView | null
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
  price_basis: 'split_adjusted_close'
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

export interface StockAnalysisRowView {
  candidate: StockCandidateContextView
  listing_span_days: number | null
  jpx_flags: string[] | null
  forecast_special_gain_flag: boolean | null
  forecast_full_year_loss_flag: boolean | null
  investment_securities: number | null
  asset_backed_ratio: number | null
  p_s_sector_gap: number | null
  operating_return_on_capital_proxy: number | null
  operating_margin: number | null
  bs_carry_forward_fields: string | null
  bs_carry_forward_lag_days: number | null
  edinet_failure_reasons: string | null
  stale_fin_flag: boolean | null
  ttm_quality_ev_ebitda: 'exact' | 'approximated' | 'unavailable' | null
  ttm_quality_fcf: 'exact' | 'approximated' | 'unavailable' | null
  next_earnings_status: 'scheduled' | 'scheduled_date_reached' | 'estimated' | 'unknown' | null
  next_earnings_estimated_date: string | null
  tse_capital_policy_status: string | null
  large_holding_filing_within_lookback: boolean | null
  tender_offer_filing_within_lookback: boolean | null
  fv_sector_median_yen: number | null
  fv_self_range_yen: number | null
  er_origin: string | null
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

export interface StockAssessmentDetailView {
  revision: ResearchRevisionView
  document: Record<string, unknown>
  review: Record<string, unknown>
  core_sha256: string
  projection: Record<string, unknown>
}

export interface StockAssessmentsView {
  assessments: ResearchRevisionView[]
  next_cursor: string | null
}

export interface StockCandidateContextView {
  common_eligible: boolean
  reasons: CandidateReasonView[]
  normalized_per_3fy_sector_gap: number | null
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
  purpose: 'new' | 'holding'
  unfinished_reason: string
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
