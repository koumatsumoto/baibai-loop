// Generated from web/backend/src/baibai_web/readmodel/models.py.
// Run `uv run python -m baibai_web.contracts_export` after changing those models;
// `tools/quality/drift/check_readmodel_contract.py` refuses a stale copy.

export interface AllocationAlternativeView {
  ticker: string
  disposition: string
  rationale: string
  thesis_id: string
  thesis_review_id: string | null
  permanent_loss_conclusion: string | null
  five_year_base_cagr_pct: number | null
  fair_value_yen: number | null
  fv_gap_pct: number | null
}

export interface AssessmentReviewView {
  attempt: number
  reviewer_identity: string
  reviewed_at: string
  conclusion: string
  open_findings: string[]
}

export interface CapitalAllocationAssessmentSummaryView {
  capital_allocation_assessment_id: string
  as_of: string
  published_at: string
  decision: string
  headline: string
  research_triage_id: string
  alternative_count: number
  allocated_ticker: string | null
}

export interface CapitalAllocationAssessmentView {
  capital_allocation_assessment_id: string
  as_of: string
  published_at: string
  decision: string
  headline: string
  research_triage_id: string
  macro_context_id: string | null
  comparison: string
  foregone_alternatives: string
  alternatives: AllocationAlternativeView[]
  content_review: AssessmentReviewView
}

/**
 * What changed between the latest machine run and the one before it.
 *
 * Every field is an observation or a comparison of observations. The view names no
 * cause and carries no recommendation: it tells the reader where to look, and the
 * decision to start a research cycle or a Position Review stays human.
 *
 * ``unavailable`` lists the sections no store could answer, so an empty section is
 * never read as "nothing changed". ``pool`` names which machine pool the comparison
 * used, and ``rules_changed`` marks a pair of runs built from different screening
 * rules — the pool difference is then a method change, so no rows are reported.
 * Holdings that cannot be compared are counts rather than rows: repeating the same
 * list every day would bury the day's actual changes. ``er_moves_total`` says how
 * many names cleared the threshold before the row cap, so a capped list does not
 * hide its own remainder.
 */
export interface DailyDeltaView {
  generated_at: string
  asof: string | null
  previous_asof: string | null
  pool: DeltaPool | null
  rules_changed: boolean
  entered: ReviewSetEntryDeltaView[]
  exited: ReviewSetEntryDeltaView[]
  er_moves: ReviewSetExpectedReturnDeltaView[]
  er_moves_total: number
  holdings: HoldingDeltaView[]
  holdings_without_fair_value: number
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
  cash_pct: number | null
  reserved_pct: number | null
  deployed_pct: number | null
  holdings: HoldingView[]
  reservations: ReservationView[]
  warnings: WarningView[]
  research_load_errors: string[]
}

export type DeltaPool = 'review_set'

export type DeltaUnavailable = 'screening_run' | 'previous_screening_run' | 'review_set' | 'review_set_estimate' | 'holdings' | 'holdings_fair_value' | 'market'

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
  asof_start: string
  asof_end: string
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

/**
 * One open holding whose observation crossed a threshold worth reading.
 *
 * ``at_or_above_fair_value`` is the comparison of two numbers, not a decision:
 * reaching fair value is a review trigger the human owns.
 */
export interface HoldingDeltaView {
  ticker: string
  company_name: string | null
  at_or_above_fair_value: boolean | null
  change_since_previous_pct: number | null
  days_to_next_earnings: number | null
}

export interface HoldingView {
  ticker: string
  company_name: string | null
  sector: string
  quantity: number
  deployed_cost_yen: number
  market_price_yen: string
  market_price_as_of: string
  market_value_yen: number
  unrealized_pnl_yen: number
  unrealized_pnl_pct: number
  fair_value_yen: number | null
  fv_gap_pct: number | null
  latest_thesis_id: string | null
  recommendation: string | null
  next_earnings_date: string | null
}

export interface MacroConnectionSectionView {
  section_id: string
  series: MacroSeriesReferenceView[]
  core_section_ids: string[]
  fact_summary: MacroFactSummaryView[]
  judgment: MacroSectionJudgmentView
  research_priority_hints: MacroResearchPriorityHintView[]
  sector_tilts: MacroSectorTiltView[]
  sizing_cautions: MacroSizingCautionView[]
  bargain_topography: MacroFactSummaryView | null
  estimate_caveats: MacroEstimateCaveatView[]
}

/**
 * Existing L3 judgment fields shown before the current L2 readings.
 */
export interface MacroContextExcerptView {
  context_id: string
  as_of: string
  published_at: string
  age_days: number
  stale: boolean
  summary: string
  synthesis: MacroSynthesisView | null
  risk_environment: MacroRiskEnvironmentView | null
  scenarios: MacroScenarioView[]
  material_deltas: MacroMaterialDeltaView[]
  research_priority_hints: MacroResearchPriorityHintView[]
  bargain_topography: MacroFactSummaryView | null
  estimate_caveats: MacroEstimateCaveatView[]
  sizing_cautions: MacroSizingCautionView[]
  warnings: string[]
}

export interface MacroContextRevisionView {
  context_id: string
  as_of: string
  published_at: string
  summary: string
  age_days: number
  stale: boolean
}

export interface MacroContextView {
  context_id: string
  as_of: string
  published_at: string
  summary: string
  age_days: number
  stale: boolean
  synthesis: MacroSynthesisView | null
  core: MacroCoreSectionView[]
  connection: MacroConnectionSectionView
}

export interface MacroCoreSectionView {
  section_id: string
  series: MacroSeriesReferenceView[]
  fact_summary: MacroFactSummaryView[]
  judgment: MacroSectionJudgmentView
  economic_connection: MacroEconomicConnectionView
  change_since_previous: string | null
  previous_scorecard_review: string | null
  material_deltas: MacroMaterialDeltaView[]
  risk_environment: MacroRiskEnvironmentView | null
  scenarios: MacroScenarioView[]
  monitoring_points: MacroMonitoringPointView[]
}

export interface MacroDominantForceView {
  force_id: string
  title: string
  summary: string
  transmission: string
  core_section_ids: string[]
  series: MacroSeriesReferenceView[]
  counter_evidence: string
  direction: string
  confidence: string
  source_ids: string[]
}

export interface MacroEconomicConnectionView {
  summary: string
  source_ids: string[]
}

export interface MacroEstimateCaveatView {
  summary: string
  applies_to: string
  affected_component: string
  materiality: string
  source_ids: string[]
}

export interface MacroFactSummaryView {
  summary: string
  source_ids: string[]
}

export interface MacroForceInteractionView {
  summary: string
  force_ids: string[]
  source_ids: string[]
}

export interface MacroGroupView {
  title: string
  series: MacroSeriesView[]
}

export interface MacroMaterialDeltaView {
  channel: string
  direction: string
  materiality: string
  summary: string
  used_for: string
  source_ids: string[]
}

export interface MacroMonitoringPointView {
  event: string
  condition: string
  view_change: string
  summary: string
  source_ids: string[]
}

export interface MacroPointView {
  observed_at: string
  value: number
}

export interface MacroReadingSeriesView {
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
  short_trend: MacroReadingTrendView | null
  long_trend: MacroReadingTrendView | null
  flags: string[]
}

export interface MacroReadingTrendView {
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
export interface MacroReadingView {
  asof: string
  rules_revision: string
  series: MacroReadingSeriesView[]
  fetch_health: MacroSeriesFetchHealthView[]
}

export interface MacroResearchPriorityHintView {
  summary: string
  applies_to: string
  source_ids: string[]
}

export interface MacroRiskEnvironmentView {
  stance: string
  confidence: string
  summary: string
  falsifiers: string[]
  source_ids: string[]
}

export interface MacroScenarioView {
  case: string
  direction: string
  probability: number | null
  summary: string
  conditions: string[]
  scorecard: MacroScorecardConditionView[]
  economic_implications: string[]
  source_ids: string[]
}

export interface MacroScorecardConditionView {
  series_id: string
  comparison: string
  threshold: number
  deadline: string
}

export interface MacroSectionJudgmentView {
  summary: string
  direction: string
  confidence: string
  source_ids: string[]
}

export interface MacroSectorTiltView {
  sector: string
  direction: string
  summary: string
  source_ids: string[]
}

/**
 * The latest acquisition attempt for one series (not part of the reading itself).
 */
export interface MacroSeriesFetchHealthView {
  series_id: string
  status: string
  finished_at: string
  record_count: number
  error_message: string | null
}

export interface MacroSeriesReferenceView {
  series_id: string
  name: string
}

export interface MacroSeriesView {
  series_id: string
  label: string
  name: string
  unit: string
  tradingview_symbol: string | null
  points: MacroPointView[]
}

export interface MacroSizingCautionView {
  severity: string
  summary: string
  source_ids: string[]
}

/**
 * The integrated layer: named cross-channel forces and how they combine.
 */
export interface MacroSynthesisView {
  dominant_forces: MacroDominantForceView[]
  interactions: MacroForceInteractionView[]
}

/**
 * Current L3 judgment first, followed by current L2 readings.
 */
export interface MacroView {
  latest_context: MacroContextExcerptView | null
  reading: MacroReadingView | null
  reports: MacroContextRevisionView[]
  groups: MacroGroupView[]
}

export type MetaBatch = 'daily' | 'manual'

/**
 * Store freshness shown alongside every view and exported as views/meta.json.
 */
export interface MetaView {
  generated_at: string
  data_updated_at: string | null
  screening_asof: string | null
  macro_asof: string | null
  app_db_updated_at: string | null
  batch: MetaBatch | null
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
}

export interface OperationsView {
  operations: OperationSessionView[]
  outcomes: PortfolioOutcomeView[]
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
  replacement_thesis_id: string | null
  action: string
  note: string | null
}

export interface ResearchRevisionView {
  as_of: string
  thesis_id: string
  recommendation: string
  confidence: string | null
  current_fair_value_yen: number | null
  model_version: string | null
  review_id: string | null
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
  ticker: string
  sector: string
  remaining_quantity: number
  price_guard_yen: string
  reserved_yen: number
  expires_at: string
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
  latest_large_holding_filing_date: string | null
  tender_offer_filing_within_lookback: boolean | null
  latest_tender_offer_filing_date: string | null
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
}

/**
 * A ticker whose presence in the machine pool changed between two runs.
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
 * A ticker in both pools whose machine E[r] moved most.
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
  p_s_sector_gap: number
  operating_return_on_capital_proxy: number
  sales_yoy: number
  operating_margin: number
  fcf_yield: number
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

export interface ScenarioView {
  name: string
  horizon_years: number
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
  entry_price_basis_yen: number | null
  required_5y_base_cagr_pct: number | null
  permanent_loss_risk_count: number
  scenarios: ScenarioView[]
  permanent_loss_conclusion: string | null
  strongest_countercase: string | null
  sizing_action: string | null
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
