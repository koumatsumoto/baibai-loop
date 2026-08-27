// Generated from web/backend/src/baibai_web/readmodel/models.py.
// Run `uv run python -m baibai_web.contracts_export` after changing those models;
// `tools/quality/drift/check_readmodel_contract.py` refuses a stale copy.

export interface AssessmentCaseView {
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

export interface BargainAssessmentSummaryView {
  assessment_id: string
  as_of: string
  published_at: string
  result: string
  headline: string
  shortlist_id: string
  case_count: number
  selected_ticker: string | null
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
  cases: AssessmentCaseView[]
  purchase: AssessmentPurchaseView | null
  review: AssessmentReviewView
}

/**
 * A ticker whose presence in the machine pool changed between two runs.
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
 * A ticker in both pools whose machine E[r] moved most.
 */
export interface CandidateMoveDeltaView {
  ticker: string
  company_name: string | null
  er_annual_pct: number | null
  previous_er_annual_pct: number | null
  change_pp: number
}

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

/**
 * What changed between the latest machine run and the one before it.
 *
 * Every field is an observation or a comparison of observations. The view names no
 * cause and carries no recommendation: it tells the reader where to look, and the
 * decision to start an opportunity cycle or a holding review stays human.
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
  upcoming_events: UpcomingEventView[]
  open_tasks: TaskView[]
  next_task: TaskView | null
  tasks_exist: boolean
  research_load_errors: string[]
}

export type DeltaPool = 'longlist' | 'recommendations'

export type DeltaUnavailable = 'candidates' | 'candidates_estimate' | 'candidates_pool' | 'candidates_previous_run' | 'holdings' | 'holdings_fair_value' | 'macro' | 'market'

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
 * Read-only warning provenance; it never carries selection authority.
 */
export interface FvConvergenceView {
  status: 'warning' | 'clear' | 'not_evaluable'
  warning_code: string | null
  market_price_yen: number | null
  anchors_yen: Record<string, number>
  er_reversion_annual: number | null
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

export interface HoldingReviewView {
  holding_review_id: string
  as_of: string
  thesis_id: string
  candidate_thesis_id: string | null
  action: string
  note: string | null
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

export interface MachineSelectionView {
  selection_id: string
  run_revision_id: string
  profile: string
  macro_context_id: string | null
  created_at: string
  longlist: SelectionLonglistEntryView[]
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

/**
 * A series whose |z-score| arrived at the distribution edge.
 *
 * ``previous_z_score`` is what it was on the earlier reading, so the reader can see
 * how far it came rather than only that it is past the line.
 */
export interface MacroExtremeDeltaView {
  series_id: string
  z_score: number
  previous_z_score: number | null
}

export interface MacroFactSummaryView {
  summary: string
  source_ids: string[]
}

/**
 * A threshold note that appeared or disappeared between two readings.
 */
export interface MacroFlagDeltaView {
  series_id: string
  flag: string
  state: 'raised' | 'cleared'
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
 * Macro overview: the report index (summaries) plus the indicator panel.
 *
 * Full report sections are served per revision by ``MacroContextView`` at
 * ``/api/macro/context/{context_id}`` so the overview stays a lightweight index.
 */
export interface MacroView {
  as_of: string
  period: '1y' | '5y' | '10y' | 'max'
  granularity: 'daily' | 'weekly' | 'monthly' | 'yearly'
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
  proposals: ProposalView[]
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

export interface ProposalView {
  proposal_id: string
  ticker: string
  thesis_id: string
  review_id: string
  created_at: string
  status: string
  decided_at: string | null
  payload: Record<string, unknown>
}

export interface ResearchQuestionView {
  question: string
  answer: string
  status: string
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

export interface ReservationView {
  reservation_id: string
  ticker: string
  sector: string
  remaining_quantity: number
  price_guard_yen: string
  reserved_yen: number
  expires_at: string
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
  rows: CandidateRowView[]
}

export interface ScreeningHistoryView {
  dates: string[]
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

export interface ScreeningView {
  run: ScreeningRunView | null
  rows: CandidateRowView[]
  selections: MachineSelectionView[]
  shortlists: ShortlistView[]
  assessments: BargainAssessmentSummaryView[]
  er_level_calibration: ErLevelCalibrationContextView | null
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

/**
 * 機械 rank 上位の候補 1 件。FV アンカーと E[r] はここだけが持つ。
 */
export interface SelectionLonglistEntryView {
  rank: number | null
  ticker: string
  name: string | null
  market_price_yen: number | null
  fair_value_anchor_yen: number | null
  fair_value_gap_pct: number | null
  expected_return_pct: number | null
  primary_evidence_pattern_id: string | null
  liquidity_status: string | null
  selection_reasons: string[]
  durability_warnings: string[]
  event_warnings: string[]
  fv_convergence: FvConvergenceView
}

export interface ShortlistEntryView {
  ticker: string
  decision: string
  reason: string
  rank: number | null
  narrative: ShortlistNarrativeView | null
  machine_snapshot: SelectionLonglistEntryView | null
}

/**
 * 発行済み shortlist の判断。read 側は欠けた field を空欄として通す。
 *
 * 必須性を強制するのは publish の write path だけであり、read model が同じ必須を
 * 課すと、schema を進めた瞬間に旧 revision を読む export と API が落ちる。
 */
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

export interface ShortlistView {
  shortlist_id: string
  selection_id: string
  run_revision_id: string
  as_of: string
  published_at: string
  entries: ShortlistEntryView[]
  unreadable_entries: number
}

export interface SourceCaveatView {
  source_id: string
  status: string
  decision_impact: string
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
