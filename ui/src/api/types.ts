export interface MetaView {
  generated_at: string
  data_updated_at?: string | null
  screening_asof: string | null
  macro_asof: string | null
  app_db_updated_at: string | null
  batch: 'daily' | 'manual' | null
}

export type SystemStoreName = 'market' | 'runs' | 'macro' | 'baibai'

export interface SystemStoreView {
  store: SystemStoreName
  exists: boolean
  size_bytes: number | null
  row_count: number | null
  latest_date: string | null
  updated_at: string | null
}

export interface SystemProviderView {
  series_id: string
  name: string
  consecutive_failures: number
  failing_since: string
  last_error: string | null
}

export interface SystemView {
  generated_at: string
  batch: 'daily' | 'manual' | null
  stores: SystemStoreView[]
  failing_providers: SystemProviderView[]
  never_attempted_series: string[]
  provider_series_total: number
}

// The workflow's terminal summary, published to R2 by the daily batch. Absent
// until the first run publishes one, and absent from the local API entirely.
export type RunOutcome =
  | 'succeeded'
  | 'skipped_non_business_day'
  | 'published_with_deferred_failure'
  | 'failed'
  // GitHub reports a job that hit `timeout-minutes` as cancelled, not failed.
  | 'cancelled'

export type RunPublishState = 'not_generated' | 'generated' | 'upload_failed' | 'published'

export interface RunErrorView {
  code: string
  stage: string
  impact: 'failed' | 'degraded'
  message: string
}

export interface RunBatchView {
  batch_name: string
  datasets: string[]
  status: 'ok' | 'degraded' | 'failed' | 'skipped'
  duration_seconds: number
  metrics: Record<string, unknown>
  errors: RunErrorView[]
}

export interface RunExecutionSummaryView {
  schema_version: number
  asof: string
  outcome: RunOutcome
  started_at: string
  finished_at: string
  duration_seconds: number
  batches: RunBatchView[]
  local_export: boolean
}

// Discriminated by `kind`; only the member for that kind is serialized, so the
// other keys are absent rather than null.
export type RunExecutionView =
  | { kind: 'available'; summary?: RunExecutionSummaryView }
  | { kind: 'not_started'; stage?: string }
  | { kind: 'unavailable'; error?: RunErrorView }

export interface WorkflowRunSummaryView {
  schema_version: number
  workflow: string
  repository: string
  trigger: string
  run_attempt: string
  run_url: string
  asof: string | null
  // When the run reached its terminal state. Without it a stale object (the
  // upload is best-effort) is indistinguishable from a fresh run.
  finished_at: string
  duration_seconds: number
  overall_outcome: RunOutcome
  publish_state: RunPublishState
  execution: RunExecutionView
  delivery: { status: string; detail: string | null }
  workflow_errors: RunErrorView[]
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

export interface ReservationView {
  reservation_id: string
  ticker: string
  sector: string
  remaining_quantity: number
  price_guard_yen: string
  reserved_yen: number
  expires_at: string
}

export interface WarningView {
  code: string
  scope: string
  key: string
  actual_pct: number
  warning_pct: number
  overridden: boolean
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

export interface UpcomingEventView {
  event_date: string
  kind: 'earnings' | 'reservation_expiry'
  ticker: string | null
  label: string
  days_until: number
}

export interface DashboardView {
  generated_at: string
  ledger_exists: boolean
  ledger_error: string | null
  ledger_as_of: string | null
  ledger_stale: boolean
  valuation_as_of?: string | null
  valuation_stale?: boolean
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
  upcoming_events: UpcomingEventView[]
  open_tasks: TaskView[]
  next_task: TaskView | null
  tasks_exist: boolean
  research_load_errors: string[]
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

export interface ProposalView {
  proposal_id: string
  ticker: string
  thesis_id: string
  review_id: string
  created_at: string
  status: 'pending' | 'approved' | 'deferred' | 'rejected'
  decided_at: string | null
  payload: Record<string, unknown>
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

export interface OperationsView {
  operations: OperationSessionView[]
  proposals: ProposalView[]
  outcomes: PortfolioOutcomeView[]
}

export interface ScreeningRunView {
  run_id: string
  run_date: string
  asof_date: string
  run_at: string
  universe_size: number
  candidate_count: number
  run_revision_id: string
  stale: boolean
}

export type PortfolioState = 'unheld' | 'held' | 'reserved' | 'held_and_reserved'

export interface CandidateRowView {
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
  margin_long_to_adv: number | null
  margin_long_share: number | null
  margin_long_delta_26w: number | null
  margin_std_long_share: number | null
  next_earnings_date: string | null
  data_quality_flags: string[]
  portfolio_state: PortfolioState
  has_research: boolean
  // FV アンカーは machine selection の longlist だけが持つので、longlist 外は null。
  fair_value_anchor_yen: number | null
  fair_value_gap_pct: number | null
}

export interface ScreeningView {
  run: ScreeningRunView | null
  rows: CandidateRowView[]
  selections: MachineSelectionView[]
  shortlists: ShortlistView[]
  assessments: BargainAssessmentSummaryView[]
}

export interface BargainAssessmentSummaryView {
  assessment_id: string
  as_of: string
  published_at: string
  result: string
  headline: string
  shortlist_id: string
  lane_count: number
  selected_ticker: string | null
}

export interface SourceCaveatView {
  source_id: string
  status: string
  decision_impact: string
}

export interface ResearchQuestionView {
  question: string
  answer: string
  status: string
}

export interface AssessmentLaneView {
  ticker: string
  name: string | null
  disposition: string
  disposition_reason: string
  thesis_id: string
  review_id: string | null
  permanent_loss_conclusion: string | null
  adverse_risk_axes: string[]
  five_year_base_cagr_pct: number | null
  required_return_pct: number | null
  fair_value_yen: number | null
  fv_gap_pct: number | null
  base_terminal_multiple: number | null
  break_even_terminal_multiple: number | null
  terminal_multiple_buffer: number | null
  break_even_earnings_growth_pct: number | null
  earnings_growth_buffer_pp: number | null
  observed_trailing_multiple: number | null
  business_model: string
  value_capture: string
  growth_quality: string
  financial_resilience: string
  strongest_countercase: string
  catalyst: string
  research_questions: ResearchQuestionView[]
  unknowns: string[]
  source_caveats: SourceCaveatView[]
}

export interface AssessmentPurchaseView {
  proposal_id: string
  ticker: string
  limit_price_yen: number
  quantity: number
  notional_yen: number
  max_acceptable_price_yen: number
  close_yen: number
  price_as_of: string
  expires_at: string
  warnings: string[]
  current_status: string | null
  superseded: boolean
}

export interface AssessmentReviewView {
  attempt: number
  reviewer_identity: string
  reviewed_at: string
  conclusion: string
  open_findings: string[]
}

export interface BargainAssessmentView {
  assessment_id: string
  as_of: string
  published_at: string
  result: string
  headline: string
  shortlist_id: string
  macro_context_id: string | null
  comparison: string
  entry_timing: string | null
  forgone: string
  lanes: AssessmentLaneView[]
  purchase: AssessmentPurchaseView | null
  review: AssessmentReviewView
}

export interface ScreeningHistoryView {
  dates: string[]
}

export interface ScreeningHistoryRunView {
  run: ScreeningRunView
  rows: CandidateRowView[]
}

export interface SelectionLonglistEntryView {
  rank: number | null
  ticker: string
  name: string | null
  market_price_yen: number | null
  fair_value_anchor_yen: number | null
  fair_value_gap_pct: number | null
  expected_return_pct: number | null
  screening_playbook: string | null
  liquidity_status: string | null
  selection_reasons: string[]
  durability_warnings: string[]
  event_warnings: string[]
  fv_convergence: FvConvergenceView
}

export interface FvConvergenceView {
  status: 'warning' | 'clear' | 'not_evaluable'
  warning_code: string | null
  market_price_yen: number | null
  anchors_yen: Record<string, number>
  er_reversion_annual: number | null
}

export interface MachineSelectionView {
  selection_id: string
  run_revision_id: string
  profile: string
  macro_context_id: string | null
  created_at: string
  longlist: SelectionLonglistEntryView[]
}

export interface ShortlistNarrativeView {
  ploss: string
  why: string
  temporary: string
  structural: string
  survive: string
  unlock: string
  counter: string
  research: string
  value: string
  prov: string
  upside: string | null
  downside: string | null
  rr: string | null
  catalyst: string | null
  catalyst_date: string | null
  macro: string | null
  sector_label: string | null
}

export interface ShortlistEntryView {
  ticker: string
  decision: string
  reason: string
  rank: number | null
  narrative: ShortlistNarrativeView | null
}

export interface ShortlistView {
  shortlist_id: string
  selection_id: string
  run_revision_id: string
  as_of: string
  published_at: string
  entries: ShortlistEntryView[]
  unreadable_entries: number
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

export interface ScenarioView {
  name: string
  horizon_years: number
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

export interface HoldingReviewView {
  holding_review_id: string
  as_of: string
  thesis_id: string
  candidate_thesis_id: string | null
  action: string
  note: string | null
}

export interface SecurityDetailView {
  ticker: string
  company_name: string | null
  sector: string | null
  holding: HoldingView | null
  revisions: ResearchRevisionView[]
  latest_thesis: ThesisDetailView | null
  holding_reviews: HoldingReviewView[]
  candidate_row: CandidateRowView | null
  candidate_run: ScreeningRunView | null
}

export interface MacroMaterialDeltaView {
  channel: string
  direction: string
  materiality: string
  summary: string
  used_for: string
  source_ids: string[]
}

export interface MacroSizingCautionView {
  severity: string
  summary: string
  source_ids: string[]
}

export interface MacroSeriesReferenceView {
  series_id: string
  name: string
}

export interface MacroFactSummaryView {
  summary: string
  source_ids: string[]
}

export interface MacroSectionJudgmentView {
  summary: string
  direction: string
  confidence: string
  source_ids: string[]
}

export interface MacroEconomicConnectionView {
  summary: string
  source_ids: string[]
}

export interface MacroRiskEnvironmentView {
  stance: string
  confidence: string
  summary: string
  falsifiers: string[]
  source_ids: string[]
}

/** A machine-checkable scenario condition: series compared to a threshold by a deadline. */
export interface MacroScorecardConditionView {
  series_id: string
  comparison: string
  threshold: number
  deadline: string
}

export interface MacroScenarioView {
  case: string
  direction: string
  /** Subjective weight on this case; null on revisions published before the field. */
  probability: number | null
  summary: string
  conditions: string[]
  scorecard: MacroScorecardConditionView[]
  economic_implications: string[]
  source_ids: string[]
}

/** A named force driving the environment across at least two transmission channels. */
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

export interface MacroForceInteractionView {
  summary: string
  force_ids: string[]
  source_ids: string[]
}

/** The integrated layer: dominant forces and how they compound or offset. */
export interface MacroSynthesisView {
  dominant_forces: MacroDominantForceView[]
  interactions: MacroForceInteractionView[]
}

/** How the current regime bends the loop's own machine estimates. */
export interface MacroEstimateCaveatView {
  summary: string
  applies_to: string
  affected_component: string
  materiality: string
  source_ids: string[]
}

export interface MacroResearchPriorityHintView {
  summary: string
  // Which candidate type the hint bites on; a hint without it has no discriminating power.
  applies_to: string
  source_ids: string[]
}

export interface MacroSectorTiltView {
  sector: string
  direction: string
  summary: string
  source_ids: string[]
}

export interface MacroMonitoringPointView {
  event: string
  condition: string
  view_change: string
  summary: string
  source_ids: string[]
}

/** One of the 10 core sections: a use-case agnostic reading of the environment. */
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

/** The single connection section, where loop-specific vocabulary is isolated. */
export interface MacroConnectionSectionView {
  section_id: string
  series: MacroSeriesReferenceView[]
  core_section_ids: string[]
  fact_summary: MacroFactSummaryView[]
  judgment: MacroSectionJudgmentView
  research_priority_hints: MacroResearchPriorityHintView[]
  sector_tilts: MacroSectorTiltView[]
  sizing_cautions: MacroSizingCautionView[]
  /** Absent (null) on revisions published before the integrated layer. */
  bargain_topography: MacroFactSummaryView | null
  estimate_caveats: MacroEstimateCaveatView[]
}

export interface MacroContextView {
  context_id: string
  as_of: string
  published_at: string
  summary: string
  age_days: number
  stale: boolean
  /** Absent (null) on revisions published before the integrated layer. */
  synthesis: MacroSynthesisView | null
  core: MacroCoreSectionView[]
  connection: MacroConnectionSectionView
}

export interface MacroContextRevisionView {
  context_id: string
  as_of: string
  published_at: string
  summary: string
  age_days: number
  stale: boolean
}

export interface MacroReadingTrendView {
  months: number
  anchor_observed_at: string
  anchor_value: number
  change: number
  direction: string
}

/** One series' machine reading: where it stands, which way it moved, how old it is. */
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
  // Mechanical estimate from the series cadence and publication lag; not an event calendar.
  next_print_estimate: string | null
  // Days from the reading as-of; negative means the next print is expected already.
  print_due_in_days: number | null
  window_years: number
  window_observations: number
  // Observations the frequency implies for the window; null for a daily series, where
  // the count depends on the market calendar rather than on the frequency.
  expected_observations: number | null
  // The effective window is not met, so percentile / z_score are withheld as null.
  insufficient_history: boolean
  // What percentile / z_score rank: 'level' for a value with a scale of its own, 'yoy'
  // for the year-on-year percent change of a series whose scale is its own history.
  statistic: string
  statistic_unit: string
  // The ranked statistic itself; null when the transform cannot reach the latest
  // observation, which withholds the rank too.
  statistic_value: number | null
  // Share of the window's statistic sample at or below statistic_value, as 0–1.
  percentile: number | null
  z_score: number | null
  short_trend: MacroReadingTrendView | null
  long_trend: MacroReadingTrendView | null
  // Notes that a textbook threshold is touched. Not a signal.
  flags: string[]
}

/** The latest acquisition attempt for one series. Not part of the reading itself. */
export interface MacroSeriesFetchHealthView {
  series_id: string
  status: string
  finished_at: string
  record_count: number
  error_message: string | null
}

export interface MacroReadingView {
  asof: string
  rules_revision: string
  series: MacroReadingSeriesView[]
  // Staleness alone cannot see a provider that has just gone silent: a low-frequency
  // series stays inside its threshold for weeks after its source stops answering.
  fetch_health: MacroSeriesFetchHealthView[]
}

export interface MacroPointView {
  observed_at: string
  value: number
}

export interface MacroSeriesView {
  series_id: string
  label: string
  name: string
  unit: string
  tradingview_symbol: string | null
  points: MacroPointView[]
}

export interface MacroGroupView {
  title: string
  series: MacroSeriesView[]
}

export interface MacroView {
  as_of: string
  period: '1y' | '5y' | '10y' | 'max'
  granularity: 'daily' | 'weekly' | 'monthly' | 'yearly'
  reports: MacroContextRevisionView[]
  groups: MacroGroupView[]
}

export type DeltaPool = 'longlist' | 'recommendations'
export type DeltaUnavailable =
  | 'candidates'
  | 'candidates_estimate'
  | 'candidates_pool'
  | 'candidates_previous_run'
  | 'holdings'
  | 'holdings_fair_value'
  | 'macro'
  | 'market'

export interface CandidateEntryDeltaView {
  ticker: string
  company_name: string | null
  er_annual_pct: number | null
  disclosed_since_previous: boolean | null
}

export interface CandidateMoveDeltaView {
  ticker: string
  company_name: string | null
  er_annual_pct: number | null
  previous_er_annual_pct: number | null
  change_pp: number
}

export interface HoldingDeltaView {
  ticker: string
  company_name: string | null
  at_or_above_fair_value: boolean | null
  change_since_previous_pct: number | null
  days_to_next_earnings: number | null
}

export interface MacroFlagDeltaView {
  series_id: string
  flag: string
  state: 'raised' | 'cleared'
}

export interface MacroExtremeDeltaView {
  series_id: string
  z_score: number
  previous_z_score: number | null
}

export interface DailyDeltaView {
  generated_at: string
  asof: string | null
  previous_asof: string | null
  pool: DeltaPool | null
  rules_changed: boolean
  entered: CandidateEntryDeltaView[]
  exited: CandidateEntryDeltaView[]
  er_moves: CandidateMoveDeltaView[]
  er_moves_total: number
  holdings: HoldingDeltaView[]
  holdings_without_fair_value: number
  holdings_without_price: number
  macro_flags: MacroFlagDeltaView[]
  macro_extremes: MacroExtremeDeltaView[]
  unavailable: DeltaUnavailable[]
}
