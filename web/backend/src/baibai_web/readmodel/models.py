"""JSON DTOs exposed only to the bundled read-only UI."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

type MetaBatch = Literal["daily", "manual"]


class MetaView(BaseModel):
    """Store freshness shown alongside every view and exported as views/meta.json."""

    generated_at: datetime
    data_updated_at: datetime | None
    screening_asof: date | None
    macro_asof: date | None
    app_db_updated_at: datetime | None
    batch: MetaBatch | None


class HoldingView(BaseModel):
    ticker: str
    company_name: str | None
    sector: str
    quantity: int
    deployed_cost_yen: int
    market_price_yen: str
    market_price_as_of: datetime
    market_value_yen: int
    unrealized_pnl_yen: int
    unrealized_pnl_pct: float
    fair_value_yen: float | None
    fv_gap_pct: float | None
    latest_thesis_id: str | None
    recommendation: str | None
    next_earnings_date: str | None = None


class ReservationView(BaseModel):
    reservation_id: str
    ticker: str
    sector: str
    remaining_quantity: int
    price_guard_yen: str
    reserved_yen: int
    expires_at: datetime


class WarningView(BaseModel):
    code: str
    scope: str
    key: str
    actual_pct: float
    warning_pct: float
    overridden: bool


class TaskView(BaseModel):
    task_id: str
    title: str
    kind: str
    status: str
    ticker: str | None
    # When to act. This is what fires the trigger and what the task list orders by.
    due_date: date
    # What the task is waiting on — a distinct concept from the deadline, kept even
    # where the two happen to hold the same date. The Dashboard renders the deadline
    # alone today; the event stays in the projection because it answers "why is this
    # dated" and is the task's own context, not a display leftover.
    event_label: str | None
    event_date: date | None
    overdue: bool


class UpcomingEventView(BaseModel):
    event_date: date
    kind: Literal["earnings", "reservation_expiry"]
    ticker: str | None
    label: str
    days_until: int


class DashboardView(BaseModel):
    generated_at: datetime
    ledger_exists: bool
    ledger_error: str | None
    ledger_as_of: datetime | None
    ledger_stale: bool
    valuation_as_of: datetime | None
    valuation_stale: bool
    total_capital_yen: int | None
    available_cash_yen: int | None
    reserved_cash_yen: int | None
    holdings_market_value_yen: int | None
    deployed_cost_yen: int | None
    cash_pct: float | None
    reserved_pct: float | None
    deployed_pct: float | None
    holdings: list[HoldingView]
    reservations: list[ReservationView]
    warnings: list[WarningView]
    upcoming_events: list[UpcomingEventView]
    open_tasks: list[TaskView]
    next_task: TaskView | None
    tasks_exist: bool
    research_load_errors: list[str]


class OperationSessionView(BaseModel):
    operation_id: str
    session_kind: str
    status: str
    as_of: date
    ticker: str | None
    started_at: datetime
    completed_at: datetime | None
    payload: dict[str, object]


class PortfolioOutcomeView(BaseModel):
    outcome_id: str
    horizon: str
    period_start_date: date
    period_end_date: date
    status: str
    reason: str | None = None
    portfolio_twr_pct: float | None = None
    benchmark_cumulative_return_pct: float | None = None


class OperationsView(BaseModel):
    operations: list[OperationSessionView]
    outcomes: list[PortfolioOutcomeView]


class ScreeningRunView(BaseModel):
    run_id: str
    run_date: date
    asof_date: date
    run_at: datetime
    universe_size: int
    analyzed_security_count: int
    # The revision the review_sets in the same payload bind to; ``run_id`` is the
    # public identifier a reader sees.
    run_revision_id: str
    stale: bool


type PortfolioState = Literal["unheld", "held", "reserved", "held_and_reserved"]


class SecurityAnalysisRowView(BaseModel):
    ticker: str
    name: str | None
    sector_33: str | None
    market_cap_oku: float | None
    avg_turnover_oku: float | None
    per_trailing: float | None
    normalized_per_3fy: float | None
    per_forward: float | None
    pbr: float | None
    ev_ebitda: float | None
    p_s: float | None
    pcfr: float | None
    dividend_yield: float | None
    # 配当利回りが空である理由。unresolved_split_basis は分割・併合を跨いだ年度で株式
    # 基準を確定できず値を出していない状態で、無配 (0) とも観測不能とも別である。
    dividend_basis: str | None = None
    dividend_split_factor: float | None = None
    er_annual: float | None
    er_reversion_annual: float | None
    er_carry_annual: float | None
    net_cash_to_market_cap: float | None
    fcf_yield: float | None
    ocf_yield: float | None
    equity_ratio: float | None
    sales_yoy: float | None
    operating_profit_yoy: float | None
    sector_relative_strength_percentile: float | None
    price_change_20d: float | None
    gap_from_52w_low: float | None
    next_earnings_date: str | None
    # 需給は観測値として表示する。margin_short_to_adv は採否基準を通過しているが
    # annotation 契約のまま warning / gate へ変換しない。制度期日偏重だけは別途検証済み
    # の閾値に達したとき flag を持つ。
    margin_week_end: date | None = None
    margin_long_to_adv: float | None
    margin_short_to_adv: float | None = None
    margin_long_share: float | None
    margin_long_delta_26w: float | None
    margin_std_long_share: float | None
    data_quality_flags: list[str]
    portfolio_state: PortfolioState
    has_research: bool
    # FV アンカーは machine review_set の entries だけが持つので、entries へ入らな
    # かった候補では空になる。read-only app は FV を導出しない。
    fair_value_anchor_yen: float | None = None
    fair_value_gap_pct: float | None = None
    # Historical calibration context only. It never alters E[r], rank, or review_set.
    er_level_quintile: int | None = None
    er_meets_8_5pct_band: bool = False


class ErLevelCalibrationStatsView(BaseModel):
    median: float
    q25: float
    q10: float
    trap_rate: float
    n: int


class ErLevelCalibrationBasisView(BaseModel):
    basis: str
    ticker_equal: ErLevelCalibrationStatsView
    cohort_equal: ErLevelCalibrationStatsView


class ErLevelCalibrationBandView(BaseModel):
    band_id: str
    quintile: int | None
    lower_er_annual: float | None
    upper_er_annual: float | None
    median_predicted_er_annual: float
    cohort_count: int
    median_n: int
    bases: list[ErLevelCalibrationBasisView]


class ErLevelCalibrationHorizonView(BaseModel):
    horizon: str
    asof_start: date
    asof_end: date
    cohort_count: int
    bands: list[ErLevelCalibrationBandView]


class ErLevelCalibrationContextView(BaseModel):
    generated_at: datetime
    valid_through: date
    reference_horizon: str
    screening_rules_hash: str
    er_model_version: str
    primary_realized_basis: str
    secondary_realized_basis: str
    trap_basis: str
    horizons: list[ErLevelCalibrationHorizonView]


class ScreeningView(BaseModel):
    run: ScreeningRunView | None
    security_analyses: list[SecurityAnalysisRowView]
    review_sets: list[ReviewSetView]
    research_triages: list[ResearchTriageView]
    capital_allocation_assessments: list[CapitalAllocationAssessmentSummaryView]
    er_level_calibration: ErLevelCalibrationContextView | None = None


class ScreeningHistoryView(BaseModel):
    dates: list[date]


class ScreeningHistoryRunView(BaseModel):
    """Stable UI projection of one retained screening run."""

    run: ScreeningRunView
    rows: list[SecurityAnalysisRowView]


class FvConvergenceView(BaseModel):
    """Read-only warning provenance; it never carries review_set authority."""

    status: Literal["warning", "clear", "not_evaluable"]
    warning_code: str | None
    market_price_yen: float | None
    anchors_yen: dict[str, float]
    er_reversion_annual: float | None


class ReviewSetEntryView(BaseModel):
    """One nominated security in deterministic multi-approach review order."""

    review_position: int
    ticker: str
    name: str | None
    sector_33: str | None
    nominations: list[dict[str, object]]
    support_count: int
    rank_vector: list[int]
    analysis: dict[str, object]


class ReviewSetView(BaseModel):
    review_set_id: str
    run_revision_id: str
    created_at: datetime
    entries: list[ReviewSetEntryView]


class ResearchTriageMachineSnapshotView(BaseModel):
    """Machine coordinates frozen into one Research Triage judgment entry."""

    review_position: int
    nominations: list[dict[str, object]] | None
    support_count: int | None
    expected_return: dict[str, object] | None
    fair_value: dict[str, object] | None
    data_quality: dict[str, object] | None


class ResearchTriageEntryView(BaseModel):
    ticker: str
    decision: str
    priority: int | None = None
    rationale: str
    research_question: str | None = None
    key_risk: str | None = None
    # 判断時の機械座標。source run が prune された後もレビュー面が読めるよう、
    # publish 時に judgment へ焼き込まれた値をそのまま返す。
    machine_snapshot: ResearchTriageMachineSnapshotView | None = None


class ResearchTriageView(BaseModel):
    research_triage_id: str
    review_set_id: str
    run_revision_id: str
    as_of: date
    published_at: datetime
    entries: list[ResearchTriageEntryView]
    # 読めなかった entry の件数。0 でないレビュー面は不完全なので、そう表示する。
    unreadable_entries: int = 0


class ResearchRevisionView(BaseModel):
    as_of: date
    thesis_id: str
    recommendation: str
    confidence: str | None
    current_fair_value_yen: float | None
    model_version: str | None
    review_id: str | None


class ScenarioView(BaseModel):
    name: str
    horizon_years: int


class ThesisDetailView(BaseModel):
    revision: ResearchRevisionView
    entry_price_basis_yen: float | None
    required_5y_base_cagr_pct: float | None
    permanent_loss_risk_count: int
    scenarios: list[ScenarioView]
    permanent_loss_conclusion: str | None
    strongest_countercase: str | None
    sizing_action: str | None


class PositionReviewView(BaseModel):
    position_review_id: str
    as_of: date
    thesis_id: str
    candidate_thesis_id: str | None
    action: str
    note: str | None


class SecurityDetailView(BaseModel):
    ticker: str
    company_name: str | None
    sector: str | None
    holding: HoldingView | None
    revisions: list[ResearchRevisionView]
    latest_thesis: ThesisDetailView | None
    position_reviews: list[PositionReviewView]
    candidate_row: SecurityAnalysisRowView | None
    candidate_run: ScreeningRunView | None


class MacroMaterialDeltaView(BaseModel):
    channel: str
    direction: str
    materiality: str
    summary: str
    used_for: str
    source_ids: list[str]


class MacroSizingCautionView(BaseModel):
    severity: str
    summary: str
    source_ids: list[str]


class MacroSeriesReferenceView(BaseModel):
    series_id: str
    name: str


class MacroFactSummaryView(BaseModel):
    summary: str
    source_ids: list[str]


class MacroSectionJudgmentView(BaseModel):
    summary: str
    direction: str
    confidence: str
    source_ids: list[str]


class MacroEconomicConnectionView(BaseModel):
    summary: str
    source_ids: list[str]


class MacroRiskEnvironmentView(BaseModel):
    stance: str
    confidence: str
    summary: str
    falsifiers: list[str]
    source_ids: list[str]


class MacroScorecardConditionView(BaseModel):
    series_id: str
    comparison: str
    threshold: float
    deadline: date


class MacroScenarioView(BaseModel):
    case: str
    direction: str
    # Subjective weight on this case; None on revisions published before the field.
    probability: float | None = None
    summary: str
    conditions: list[str]
    scorecard: list[MacroScorecardConditionView]
    economic_implications: list[str]
    source_ids: list[str]


class MacroDominantForceView(BaseModel):
    force_id: str
    title: str
    summary: str
    transmission: str
    core_section_ids: list[str]
    series: list[MacroSeriesReferenceView]
    counter_evidence: str
    direction: str
    confidence: str
    source_ids: list[str]


class MacroForceInteractionView(BaseModel):
    summary: str
    force_ids: list[str]
    source_ids: list[str]


class MacroSynthesisView(BaseModel):
    """The integrated layer: named cross-channel forces and how they combine."""

    dominant_forces: list[MacroDominantForceView]
    interactions: list[MacroForceInteractionView]


class MacroEstimateCaveatView(BaseModel):
    summary: str
    applies_to: str
    affected_component: str
    materiality: str
    source_ids: list[str]


class MacroResearchPriorityHintView(BaseModel):
    summary: str
    applies_to: str
    source_ids: list[str]


class MacroSectorTiltView(BaseModel):
    sector: str
    direction: str
    summary: str
    source_ids: list[str]


class MacroMonitoringPointView(BaseModel):
    event: str
    condition: str
    view_change: str
    summary: str
    source_ids: list[str]


class MacroCoreSectionView(BaseModel):
    section_id: str
    series: list[MacroSeriesReferenceView]
    fact_summary: list[MacroFactSummaryView]
    judgment: MacroSectionJudgmentView
    economic_connection: MacroEconomicConnectionView
    change_since_previous: str | None
    previous_scorecard_review: str | None
    material_deltas: list[MacroMaterialDeltaView]
    risk_environment: MacroRiskEnvironmentView | None
    scenarios: list[MacroScenarioView]
    monitoring_points: list[MacroMonitoringPointView]


class MacroConnectionSectionView(BaseModel):
    section_id: str
    series: list[MacroSeriesReferenceView]
    core_section_ids: list[str]
    fact_summary: list[MacroFactSummaryView]
    judgment: MacroSectionJudgmentView
    research_priority_hints: list[MacroResearchPriorityHintView]
    sector_tilts: list[MacroSectorTiltView]
    sizing_cautions: list[MacroSizingCautionView]
    # Absent on revisions published before the integrated layer.
    bargain_topography: MacroFactSummaryView | None = None
    estimate_caveats: list[MacroEstimateCaveatView] = []


class MacroContextView(BaseModel):
    context_id: str
    as_of: date
    published_at: datetime
    summary: str
    age_days: int
    stale: bool
    # Absent on revisions published before the integrated layer.
    synthesis: MacroSynthesisView | None = None
    core: list[MacroCoreSectionView]
    connection: MacroConnectionSectionView


class MacroContextRevisionView(BaseModel):
    context_id: str
    as_of: date
    published_at: datetime
    summary: str
    age_days: int
    stale: bool


class MacroReadingTrendView(BaseModel):
    months: int
    anchor_observed_at: date
    anchor_value: float
    change: float
    direction: str


class MacroReadingSeriesView(BaseModel):
    series_id: str
    name: str
    category: str
    geography: str
    frequency: str
    unit: str
    latest_value: float | None
    observed_at: date | None
    staleness_days: int | None
    stale: bool
    staleness_warn_days: int
    next_print_estimate: date | None
    print_due_in_days: int | None
    window_years: int
    window_observations: int
    expected_observations: int | None
    insufficient_history: bool
    # What percentile / z_score rank: the level, or the year-on-year percent change for a
    # series whose level scale is set by its own history.
    statistic: str
    statistic_unit: str
    statistic_value: float | None
    percentile: float | None
    z_score: float | None
    short_trend: MacroReadingTrendView | None
    long_trend: MacroReadingTrendView | None
    flags: list[str]


class MacroSeriesFetchHealthView(BaseModel):
    """The latest acquisition attempt for one series (not part of the reading itself)."""

    series_id: str
    status: str
    finished_at: str
    record_count: int
    error_message: str | None


class MacroReadingView(BaseModel):
    """The machine reading of every registered series for one as-of date.

    A projection of the L2 reading, recomputed from the indicator store; the panel it
    feeds is a display of that reading and not a second canonical artifact.
    ``fetch_health`` rides along because staleness alone cannot see a provider that has
    just gone silent — a low-frequency series stays inside its threshold for weeks.
    """

    asof: date
    rules_revision: str
    series: list[MacroReadingSeriesView]
    fetch_health: list[MacroSeriesFetchHealthView]


class MacroSeriesChangeView(BaseModel):
    """One observed series value that differs between two L2 readings."""

    series_id: str
    name: str
    frequency: str
    unit: str
    previous_observed_at: date | None
    observed_at: date | None
    previous_value: float | None
    value: float | None
    value_change: float | None
    z_score_delta: float | None


class MacroStateChangeView(BaseModel):
    series_id: str
    kind: Literal["flag", "stale", "extreme"]
    state: Literal["raised", "cleared"]
    detail: str


class MacroComparisonView(BaseModel):
    from_as_of: date
    to_as_of: date
    changed_total: int
    daily_changed_total: int
    daily_moves: list[MacroSeriesChangeView]
    daily_moves_omitted: int
    non_daily_updates: list[MacroSeriesChangeView]
    state_changes: list[MacroStateChangeView]


class MacroStandingView(BaseModel):
    fetch_failed: list[MacroSeriesFetchHealthView]
    stale_series_ids: list[str]
    flagged_series_ids: list[str]
    extreme_series_ids: list[str]


class MacroMachineUpdateView(BaseModel):
    series_total: int
    fetch_ok_count: int
    fetch_failed_count: int
    previous_day: MacroComparisonView | None
    since_context: MacroComparisonView | None
    standing: MacroStandingView


class MacroContextExcerptView(BaseModel):
    """The L3 fields needed to judge today's L2 changes without opening the full report."""

    context_id: str
    as_of: date
    published_at: datetime
    age_days: int
    stale: bool
    summary: str
    synthesis: MacroSynthesisView | None
    risk_environment: MacroRiskEnvironmentView | None
    scenarios: list[MacroScenarioView]
    material_deltas: list[MacroMaterialDeltaView]
    research_priority_hints: list[MacroResearchPriorityHintView]
    bargain_topography: MacroFactSummaryView | None
    estimate_caveats: list[MacroEstimateCaveatView]
    sizing_cautions: list[MacroSizingCautionView]
    warnings: list[str]


class MacroPointView(BaseModel):
    observed_at: date
    value: float


class MacroSeriesView(BaseModel):
    series_id: str
    label: str
    name: str
    unit: str
    tradingview_symbol: str | None
    points: list[MacroPointView]


class MacroGroupView(BaseModel):
    title: str
    series: list[MacroSeriesView]


class MacroView(BaseModel):
    """One daily decision entrance: L2 change facts plus the latest L3 judgment."""

    requested_as_of: date
    data_as_of: date | None
    previous_data_as_of: date | None
    context_as_of: date | None
    rules_revision: str | None
    machine_update: MacroMachineUpdateView
    latest_context: MacroContextExcerptView | None
    reading: MacroReadingView | None
    # Older revisions only; the latest revision is the structured excerpt above.
    reports: list[MacroContextRevisionView]
    # Registry/group metadata only. Initial responses carry zero history points;
    # one series history is requested only when its dialog opens.
    groups: list[MacroGroupView]


class SourceCaveatView(BaseModel):
    source_id: str
    status: str
    decision_impact: str


class ResearchQuestionView(BaseModel):
    question: str
    answer: str
    status: str


class AllocationAlternativeView(BaseModel):
    ticker: str
    disposition: str
    rationale: str
    thesis_id: str
    thesis_review_id: str | None
    permanent_loss_conclusion: str | None
    five_year_base_cagr_pct: float | None
    fair_value_yen: float | None
    fv_gap_pct: float | None


class AssessmentReviewView(BaseModel):
    attempt: int
    reviewer_identity: str
    reviewed_at: datetime
    conclusion: str
    open_findings: list[str]


class CapitalAllocationAssessmentSummaryView(BaseModel):
    capital_allocation_assessment_id: str
    as_of: date
    published_at: datetime
    result: str
    headline: str
    research_triage_id: str
    alternative_count: int
    allocated_ticker: str | None


class CapitalAllocationAssessmentView(BaseModel):
    capital_allocation_assessment_id: str
    as_of: date
    published_at: datetime
    result: str
    headline: str
    research_triage_id: str
    macro_context_id: str | None
    comparison: str
    forgone: str
    alternatives: list[AllocationAlternativeView]
    review: AssessmentReviewView


type DeltaPool = Literal["review_set"]
type DeltaUnavailable = Literal[
    "candidates",
    "candidates_estimate",
    "candidates_pool",
    "candidates_previous_run",
    "holdings",
    "holdings_fair_value",
    "market",
]


class CandidateEntryDeltaView(BaseModel):
    """A ticker whose presence in the machine pool changed between two runs.

    ``disclosed_since_previous`` is ``null`` when the store that holds disclosure
    dates could not answer, which must not read as "no disclosure".
    """

    ticker: str
    company_name: str | None
    er_annual_pct: float | None
    disclosed_since_previous: bool | None


class CandidateMoveDeltaView(BaseModel):
    """A ticker in both pools whose machine E[r] moved most."""

    ticker: str
    company_name: str | None
    er_annual_pct: float | None
    previous_er_annual_pct: float | None
    change_pp: float


class HoldingDeltaView(BaseModel):
    """One open holding whose observation crossed a threshold worth reading.

    ``at_or_above_fair_value`` is the comparison of two numbers, not a decision:
    reaching fair value is a review trigger the human owns.
    """

    ticker: str
    company_name: str | None
    at_or_above_fair_value: bool | None
    change_since_previous_pct: float | None
    days_to_next_earnings: int | None


class DailyDeltaView(BaseModel):
    """What changed between the latest machine run and the one before it.

    Every field is an observation or a comparison of observations. The view names no
    cause and carries no recommendation: it tells the reader where to look, and the
    decision to start a research cycle or a Position Review stays human.

    ``unavailable`` lists the sections no store could answer, so an empty section is
    never read as "nothing changed". ``pool`` names which machine pool the comparison
    used, and ``rules_changed`` marks a pair of runs built from different screening
    rules — the pool difference is then a method change, so no rows are reported.
    Holdings that cannot be compared are counts rather than rows: repeating the same
    list every day would bury the day's actual changes. ``er_moves_total`` says how
    many names cleared the threshold before the row cap, so a capped list does not
    hide its own remainder.
    """

    generated_at: datetime
    asof: date | None
    previous_asof: date | None
    pool: DeltaPool | None
    rules_changed: bool
    entered: list[CandidateEntryDeltaView]
    exited: list[CandidateEntryDeltaView]
    er_moves: list[CandidateMoveDeltaView]
    er_moves_total: int
    holdings: list[HoldingDeltaView]
    holdings_without_fair_value: int
    holdings_without_price: int
    unavailable: list[DeltaUnavailable]
