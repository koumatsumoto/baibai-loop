"""JSON DTOs exposed only to the bundled read-only UI."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from baibai_engine.read_api.external_assessment import ExternalAssessment

type MetaBatch = Literal["daily", "manual"]


class MetaView(BaseModel):
    """Store freshness shown alongside every view and exported as views/meta.json."""

    generated_at: datetime
    data_updated_at: datetime | None
    screening_as_of: date | None
    indicators_as_of: date | None
    app_db_updated_at: datetime | None
    batch: MetaBatch | None


class HoldingView(BaseModel):
    ticker: str
    company_name: str | None
    sector: str
    quantity: int
    deployed_cost_yen: int
    market_price_yen: str | None
    market_price_as_of: date | None
    market_value_yen: int | None
    unrealized_pnl_yen: int | None
    unrealized_pnl_pct: float | None
    pmax_raw_yen: float | None
    pmax_gap_pct: float | None
    pmax_gap_unavailable_reason: str | None = None
    latest_assessment_id: str | None
    stance: str | None
    next_earnings_date: str | None = None


class ReservationView(BaseModel):
    reservation_id: str
    side: Literal["buy", "sell"]
    ticker: str
    sector: str
    remaining_quantity: int
    price_guard_yen: str
    reserved_yen: int
    expires_at: datetime | None
    expiry_label: str | None
    route_label: str | None


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
    # where the two happen to hold the same date. The Tasks view renders the deadline
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
    valuation_as_of: date | None
    valuation_stale: bool
    total_capital_yen: int | None
    available_cash_yen: int | None = Field(
        description="台帳上の未予約現金。売却代金は約定時にgrossで計上し、受渡済み資金や証券会社の発注可能額は表さない。"
    )
    reserved_cash_yen: int | None
    holdings_market_value_yen: int | None
    deployed_cost_yen: int | None
    realized_gross_pnl_yen: int | None
    cash_pct: float | None
    reserved_pct: float | None
    deployed_pct: float | None
    holdings: list[HoldingView]
    reservations: list[ReservationView]
    warnings: list[WarningView]


class TasksView(BaseModel):
    """Task/event workflow projection, separate from portfolio presentation."""

    generated_at: datetime
    tasks_exist: bool
    open_tasks: list[TaskView]
    next_task: TaskView | None
    upcoming_events: list[UpcomingEventView]
    ledger_error: str | None


class AssessmentRefView(BaseModel):
    assessment_id: str
    core_sha256: str
    review_id: str


class OperationResearchTargetView(BaseModel):
    selection_rationale: str | None = None
    research_question: str | None = None
    key_risk: str | None = None
    ticker: str
    purpose: Literal["new", "holding"]
    outcome_status: Literal["reviewed", "unfinished"] | None
    assessment_ref: AssessmentRefView | None
    unfinished_reason: str | None
    references: list[str]


class OperationResearchView(BaseModel):
    request_scope: str | None = None
    selection_summary: str | None = None
    max_deep_research_targets: int | None = None
    request: str
    targets: list[OperationResearchTargetView]
    proposal_id: str | None
    proposal_draft_sha256: str | None
    source_run_revision_id: str | None = None
    source_analysis_as_of: date | None = None


class OperationSessionView(BaseModel):
    operation_id: str
    session_kind: str
    status: str
    as_of: date
    ticker: str | None
    started_at: datetime
    completed_at: datetime | None
    payload: dict[str, object]
    research: OperationResearchView | None = None


class PortfolioOutcomeView(BaseModel):
    outcome_id: str
    horizon: str
    period_start_date: date
    period_end_date: date
    status: str
    reason: str | None = None
    portfolio_twr_pct: float | None = Field(
        default=None,
        description="記録済みの税・費用を反映した期間return。未報告の税・費用をゼロ確定とみなさない。",
    )
    benchmark_cumulative_return_pct: float | None = None


class OperationsView(BaseModel):
    operations: list[OperationSessionView]
    outcomes: list[PortfolioOutcomeView]


class ScreeningRunView(BaseModel):
    run_id: str
    as_of: date
    generated_at: datetime
    universe_size: int
    analyzed_security_count: int
    # The revision the candidate rows in the same payload bind to; ``run_id`` is the
    # public identifier a reader sees.
    run_revision_id: str
    stale: bool


type PortfolioState = Literal["unheld", "held", "reserved", "held_and_reserved"]


class CandidateReasonView(BaseModel):
    valuation_approach_id: Literal[
        "current-earnings-power", "normalized-earnings-power", "asset-value", "reinvestment-value"
    ]
    valuation_method_id: str
    rank: int


class StockCandidateContextView(BaseModel):
    common_eligible: bool
    reasons: list[CandidateReasonView]
    normalized_per_3fy_sector_gap: float | None


class CandidateDiscoveryMethodIdentityView(BaseModel):
    method_id: str
    method_hash: str
    nomination_depth: int


class CandidateDiscoverySummaryView(BaseModel):
    method: CandidateDiscoveryMethodIdentityView
    common_eligible_count: int
    candidate_counts_by_approach: dict[str, int]
    candidate_count: int


class StockAnalysisRowView(BaseModel):
    candidate: StockCandidateContextView
    price_history_span_days: float | None
    jpx_flags: list[str] | None
    forecast_special_gain_flag: bool | None
    forecast_full_year_loss_flag: bool | None
    investment_securities: float | None
    asset_backed_ratio: float | None
    p_s_sector_gap: float | None
    operating_return_on_capital_proxy: float | None
    operating_margin: float | None
    bs_carry_forward_fields: str | None
    bs_carry_forward_lag_days: float | None
    edinet_failure_reasons: str | None
    stale_fin_flag: bool | None
    ttm_quality_ev_ebitda: Literal["exact", "approximated", "unavailable"] | None
    ttm_quality_fcf: Literal["exact", "approximated", "unavailable"] | None
    next_earnings_status: (
        Literal["scheduled", "scheduled_date_reached", "estimated", "unknown"] | None
    )
    next_earnings_estimated_date: str | None
    tse_capital_policy_status: str | None
    large_holding_filing_within_lookback: bool | None
    tender_offer_filing_within_lookback: bool | None
    fv_sector_median_yen: float | None
    fv_self_range_yen: float | None
    er_origin: str | None
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
    net_share_change_yoy: float | None = None
    carry_missing_assumption: str | None = None
    er_model_version: str | None = None
    er_unit: str | None = None
    er_anchor_metrics: str | None = None
    er_assumptions: str | None = None
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
    # 需給は観測値として表示し、候補選定の順位や採否条件とは区別する。
    margin_balance_date: date | None = None
    margin_long_to_adv: float | None
    margin_short_to_adv: float | None = None
    margin_long_share: float | None
    margin_long_delta_26w: float | None
    margin_std_long_share: float | None
    data_quality_flags: list[str]
    portfolio_state: PortfolioState
    has_research: bool
    # 保存済みStock Analysisの機械anchor。候補入りを条件にせず再計算もしない。
    fair_value_anchor_yen: float | None = None
    fair_value_gap_pct: float | None = None
    # Historical calibration context only. It never alters E[r], rank, or candidates.
    er_level_quintile: int | None = None
    er_meets_8_5pct_band: bool = False


class ErLevelCalibrationStatsView(BaseModel):
    median: float
    q25: float
    q10: float
    trap_rate: float
    n: int


class ErLevelCalibrationCoverageView(BaseModel):
    declared_n: int
    observed_n: int
    missing_n: int
    missing_reason_counts: dict[str, int]


class ErLevelCalibrationBasisView(BaseModel):
    basis: str
    coverage: ErLevelCalibrationCoverageView
    observed_cohort_count: int
    median_n: int
    ticker_equal: ErLevelCalibrationStatsView | None
    cohort_equal: ErLevelCalibrationStatsView | None


class ErLevelCalibrationBandView(BaseModel):
    band_id: str
    quintile: int | None
    lower_er_annual: float | None
    upper_er_annual: float | None
    median_predicted_er_annual: float | None
    cohort_count: int
    median_n: int
    bases: list[ErLevelCalibrationBasisView]


class ErLevelCalibrationHorizonView(BaseModel):
    horizon: str
    as_of_start: date
    as_of_end: date
    cohort_count: int
    bands: list[ErLevelCalibrationBandView]


class ErLevelCalibrationContextView(BaseModel):
    interpretation: str
    required_asofs: list[str]
    evidence_complete: bool
    evidence_blocking_reasons: list[str]
    generated_at: datetime
    valid_through: date
    reference_horizon: str
    screening_rules_hash: str
    er_model_version: str
    primary_realized_basis: str
    secondary_realized_basis: str
    trap_basis: str
    horizons: list[ErLevelCalibrationHorizonView]


class ExternalAssessmentSecurityView(BaseModel):
    ticker: str
    name: str | None
    sector_33: str | None
    portfolio_state: PortfolioState
    has_research: bool


class ScreeningView(BaseModel):
    run: ScreeningRunView | None
    stock_analyses: list[StockAnalysisRowView]
    external_assessment_securities: list[ExternalAssessmentSecurityView] = Field(
        default_factory=list
    )
    external_assessment_cutoff: datetime | None = None
    candidate_discovery: CandidateDiscoverySummaryView | None
    investment_proposals: list[InvestmentProposalSummaryView]
    er_level_calibration: ErLevelCalibrationContextView | None = None


class ScreeningHistoryView(BaseModel):
    dates: list[date]


class ScreeningHistoryRunView(BaseModel):
    """Stable UI projection of one retained screening run."""

    run: ScreeningRunView
    rows: list[StockAnalysisRowView]


class ResearchRevisionView(BaseModel):
    ticker: str
    company_name: str
    sector: str
    summary: str
    as_of: date
    published_at: datetime
    assessment_id: str
    stance: str
    pmax_original_yen: float | None
    review_id: str
    status: str


class StockAssessmentDetailView(BaseModel):
    revision: ResearchRevisionView
    document: dict[str, Any]
    review: dict[str, Any]
    core_sha256: str
    projection: dict[str, Any]


class StockAssessmentsView(BaseModel):
    assessments: list[ResearchRevisionView]
    next_cursor: str | None = None


class MarketPointView(BaseModel):
    observation_date: date | None
    date: date
    benchmark_return_20d: float | None
    benchmark_return_60d: float | None
    breadth_pct_above_ma20: float | None
    breadth_sample_size: int
    benchmark_trend: Literal["uptrend", "downtrend", "neutral", "unknown"]


class MarketSectorView(BaseModel):
    sector_33: str
    ticker_count: int
    median_return_20d: float
    median_return_60d: float | None
    pct_above_ma20: float | None


class MarketSnapshotView(BaseModel):
    as_of: date
    benchmark_ticker: str
    history_weeks: int
    master_snapshot_date: date | None
    points: list[MarketPointView]
    sectors: list[MarketSectorView]
    return_basis: Literal["price_return_excluding_cash_dividends"] = (
        "price_return_excluding_cash_dividends"
    )


class SecurityMarketMasterView(BaseModel):
    name: str
    market_segment: str
    sector_33: str
    is_common_stock: bool
    snapshot_date: date


class SecurityPriceContextView(BaseModel):
    resolved_date: date
    close: float
    price_basis: Literal["price_adjusted_close"]
    return_1d: float | None
    return_5d: float | None
    return_20d: float | None
    return_60d: float | None
    high_52w: float
    low_52w: float
    gap_from_52w_low: float | None
    gap_from_52w_high: float | None
    self_range_percentile: float
    self_range_window_bars: int
    realized_vol_20d: float | None
    avg_turnover_20d_oku: float | None
    bar_count: int


class SecurityPeerContextView(BaseModel):
    sector_33: str
    peer_count: int
    peer_median_return_20d: float
    relative_return_20d: float | None
    comparison_start_date: date
    comparison_end_date: date
    master_snapshot_date: date | None


class SecurityRelativeContextView(BaseModel):
    benchmark_ticker: str
    relative_1d: float | None
    relative_5d: float | None
    relative_20d: float | None
    relative_60d: float | None
    sector: SecurityPeerContextView | None


class BenchmarkTrendView(BaseModel):
    as_of: date
    benchmark_ticker: str
    observation_date: date
    benchmark_return_20d: float | None
    benchmark_return_60d: float | None
    benchmark_gap_from_high: float | None
    trend_state: Literal["uptrend", "downtrend", "neutral", "unknown"]


class SecurityMarketContextView(BaseModel):
    as_of: date
    master: SecurityMarketMasterView | None
    price: SecurityPriceContextView | None
    relative: SecurityRelativeContextView | None
    benchmark_trend: BenchmarkTrendView | None
    return_basis: Literal["price_return_excluding_cash_dividends"] = (
        "price_return_excluding_cash_dividends"
    )


class SecurityDetailView(BaseModel):
    market_context: SecurityMarketContextView | None = None
    external_assessment: ExternalAssessment | None = None
    ticker: str
    company_name: str | None
    sector: str | None
    holding: HoldingView | None
    revisions: list[ResearchRevisionView]
    latest_assessment: StockAssessmentDetailView | None
    investment_proposals: list[InvestmentProposalSummaryView]
    stock_analysis: StockAnalysisRowView | None
    screening_run: ScreeningRunView | None


class HistoricalMaterialDeltaView(BaseModel):
    channel: str
    direction: str
    materiality: str
    summary: str
    used_for: str
    source_ids: list[str]


class HistoricalSizingCautionView(BaseModel):
    severity: str
    summary: str
    source_ids: list[str]


class HistoricalSeriesReferenceView(BaseModel):
    series_id: str
    name: str


class HistoricalFactSummaryView(BaseModel):
    summary: str
    source_ids: list[str]


class HistoricalSectionJudgmentView(BaseModel):
    summary: str
    direction: str
    confidence: str
    source_ids: list[str]


class HistoricalEconomicConnectionView(BaseModel):
    summary: str
    source_ids: list[str]


class HistoricalRiskEnvironmentView(BaseModel):
    stance: str
    confidence: str
    summary: str
    falsifiers: list[str]
    source_ids: list[str]


class HistoricalScorecardConditionView(BaseModel):
    series_id: str
    comparison: str
    threshold: float
    deadline: date


class HistoricalScenarioView(BaseModel):
    case: str
    direction: str
    # Subjective weight on this case; None on revisions published before the field.
    probability: float | None = None
    summary: str
    conditions: list[str]
    scorecard: list[HistoricalScorecardConditionView]
    economic_implications: list[str]
    source_ids: list[str]


class HistoricalDominantForceView(BaseModel):
    force_id: str
    title: str
    summary: str
    transmission: str
    core_section_ids: list[str]
    series: list[HistoricalSeriesReferenceView]
    counter_evidence: str
    direction: str
    confidence: str
    source_ids: list[str]


class HistoricalForceInteractionView(BaseModel):
    summary: str
    force_ids: list[str]
    source_ids: list[str]


class HistoricalSynthesisView(BaseModel):
    """The integrated layer: named cross-channel forces and how they combine."""

    dominant_forces: list[HistoricalDominantForceView]
    interactions: list[HistoricalForceInteractionView]


class HistoricalEstimateCaveatView(BaseModel):
    summary: str
    applies_to: str
    affected_component: str
    materiality: str
    source_ids: list[str]


class HistoricalResearchPriorityHintView(BaseModel):
    summary: str
    applies_to: str
    source_ids: list[str]


class HistoricalSectorTiltView(BaseModel):
    sector: str
    direction: str
    summary: str
    source_ids: list[str]


class HistoricalMonitoringPointView(BaseModel):
    event: str
    condition: str
    view_change: str
    summary: str
    source_ids: list[str]


class HistoricalCoreSectionView(BaseModel):
    section_id: str
    series: list[HistoricalSeriesReferenceView]
    fact_summary: list[HistoricalFactSummaryView]
    judgment: HistoricalSectionJudgmentView
    economic_connection: HistoricalEconomicConnectionView
    change_since_previous: str | None
    previous_scorecard_review: str | None
    material_deltas: list[HistoricalMaterialDeltaView]
    risk_environment: HistoricalRiskEnvironmentView | None
    scenarios: list[HistoricalScenarioView]
    monitoring_points: list[HistoricalMonitoringPointView]


class HistoricalConnectionSectionView(BaseModel):
    section_id: str
    series: list[HistoricalSeriesReferenceView]
    core_section_ids: list[str]
    fact_summary: list[HistoricalFactSummaryView]
    judgment: HistoricalSectionJudgmentView
    research_priority_hints: list[HistoricalResearchPriorityHintView]
    sector_tilts: list[HistoricalSectorTiltView]
    sizing_cautions: list[HistoricalSizingCautionView]
    # Absent on revisions published before the integrated layer.
    bargain_topography: HistoricalFactSummaryView | None = None
    estimate_caveats: list[HistoricalEstimateCaveatView] = []


class AssessmentClaimView(BaseModel):
    summary: str
    source_ids: list[str] = []


class AssessmentContentView(BaseModel):
    known_at: datetime
    changes: list[AssessmentClaimView] = []
    counter_evidence: list[AssessmentClaimView] = []
    uncertainties: list[AssessmentClaimView] = []
    implications: list[AssessmentClaimView] = []
    inputs: list[dict[str, Any]] = []


class AssessmentPublicationView(BaseModel):
    context_id: str
    as_of: date
    published_at: datetime
    summary: str
    age_days: int
    stale: bool
    # Absent on revisions published before the integrated layer.
    synthesis: HistoricalSynthesisView | None = None
    core: list[HistoricalCoreSectionView] = []
    connection: HistoricalConnectionSectionView | None = None
    assessment: AssessmentContentView | None = None
    historical_payload: dict[str, Any] | None = None


class AssessmentRevisionView(BaseModel):
    context_id: str
    as_of: date
    published_at: datetime
    summary: str
    age_days: int
    stale: bool


class ReadingTrendView(BaseModel):
    months: int
    anchor_observed_at: date
    anchor_value: float
    change: float
    direction: str


class StoreReadingSemanticsView(BaseModel):
    """The store reader's selection contract, not a guarantee for arbitrary pure readers."""

    mode: Literal["recomputed_from_current_store"]
    point_in_time_guaranteed: Literal[False]
    known_at_applied: Literal[False]
    limitation: str
    vintage_selection: Literal["publication_vintage_cutoff", "latest_stored_history"] | None = None
    vintage_cutoff: date | None = None


class ReadingSeriesView(BaseModel):
    read_semantics: StoreReadingSemanticsView | None = None
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
    short_trend: ReadingTrendView | None
    long_trend: ReadingTrendView | None
    flags: list[str]
    vintage_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)


class IndicatorFetchHealthView(BaseModel):
    """The latest acquisition attempt for one series (not part of the reading itself)."""

    series_id: str
    status: str
    finished_at: str
    record_count: int
    error_message: str | None


class ReadingView(BaseModel):
    """The machine reading of every registered series for one as-of date.

    A projection of the L2 reading, recomputed from the indicator store; the panel it
    feeds is a display of that reading and not a second canonical artifact.
    ``fetch_health`` rides along because staleness alone cannot see a provider that has
    just gone silent — a low-frequency series stays inside its threshold for weeks.
    """

    read_semantics: StoreReadingSemanticsView | None = None
    as_of: date
    rules_revision: str
    series: list[ReadingSeriesView]
    fetch_health: list[IndicatorFetchHealthView]


class AssessmentExcerptView(BaseModel):
    """Existing L3 judgment fields shown before the current L2 readings."""

    context_id: str
    as_of: date
    published_at: datetime
    age_days: int
    stale: bool
    summary: str
    synthesis: HistoricalSynthesisView | None
    risk_environment: HistoricalRiskEnvironmentView | None
    scenarios: list[HistoricalScenarioView]
    material_deltas: list[HistoricalMaterialDeltaView]
    research_priority_hints: list[HistoricalResearchPriorityHintView]
    bargain_topography: HistoricalFactSummaryView | None
    estimate_caveats: list[HistoricalEstimateCaveatView]
    sizing_cautions: list[HistoricalSizingCautionView]
    warnings: list[str]

    assessment: AssessmentContentView | None = None


class IndicatorPointView(BaseModel):
    observed_at: date
    value: float
    quality_flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    vintage_at: datetime | None = None


class IndicatorSeriesView(BaseModel):
    series_id: str
    label: str
    name: str
    unit: str
    tradingview_symbol: str | None
    points: list[IndicatorPointView]


class IndicatorGroupView(BaseModel):
    title: str
    series: list[IndicatorSeriesView]


class MarketContextView(BaseModel):
    """Current L3 judgment first, followed by current L2 readings."""

    market_snapshot: MarketSnapshotView | None = None

    latest_context: AssessmentExcerptView | None
    reading: ReadingView | None
    # Older revisions only; the latest revision is the structured excerpt above.
    reports: list[AssessmentRevisionView]
    # Group rows carry at most thirteen monthly period-end points for their fixed
    # overview sparkline. Full daily history remains one-series-only and lazy.
    groups: list[IndicatorGroupView]


class GateReasonView(BaseModel):
    code: str
    message: str


class LimitTermsView(BaseModel):
    """保存当時の具体条件。現在条件、注文受理、約定を推定しない。"""

    side: Literal["buy", "sell"]
    limit_price_yen: str
    quantity: int
    notional_yen: str
    target_session: date
    valuation_as_of: date
    maximum_entry_price_yen: str | None
    price_grid_rule_id: str
    conditions: list[str]
    warnings: list[str]


class InvestmentProposalItemView(BaseModel):
    ticker: str
    action: Literal["buy", "hold", "sell", "no_action", "defer"]
    rationale: str
    review_conditions: list[str]
    assessment_ref: AssessmentRefView | None = None
    timing: str | None = None
    proposed_limit_yen: str | None = None
    terms: LimitTermsView | None = None
    terms_unavailable_reasons: list[GateReasonView] = Field(default_factory=list)
    order_readiness: Literal["ready", "needs_terms", "not_applicable"]
    position: dict[str, Any] | None = None
    holding_basis: str | None = None
    case_reassessment_reason: str | None = None
    remaining_reward: dict[str, Any] | None = None
    return_calculation_unavailable_reason: str | None = None
    reason_code: str | None = None
    blockers: list[GateReasonView] = Field(default_factory=list)
    resume_conditions: list[str] = Field(default_factory=list)
    assessment_projection: dict[str, Any] | None = None
    assessment_review: dict[str, Any] | None = None


class InvestmentProposalSummaryView(BaseModel):
    proposal_id: str
    as_of: date
    published_at: datetime
    request_scope: str
    headline: str
    item_count: int
    tickers: list[str]
    actions: list[str]
    research_operation_id: str


class InvestmentProposalsView(BaseModel):
    proposals: list[InvestmentProposalSummaryView]
    next_cursor: str | None = None


class UnfinishedResearchView(BaseModel):
    ticker: str
    purpose: Literal["new", "holding"]
    unfinished_reason: str
    references: list[str]


class InvestmentProposalView(BaseModel):
    proposal_id: str
    as_of: date
    prepared_at: datetime
    published_at: datetime
    request_scope: str
    headline: str
    comparison: str
    forgone: str
    market_context_assessment_id: str | None
    research_scope: dict[str, Any]
    saved_inputs: dict[str, Any]
    items: list[InvestmentProposalItemView]
    review: dict[str, Any]
    draft_sha256: str
    liquidity_context: list[dict[str, Any]]
    warnings: list[str]
    portfolio_exposure: dict[str, Any]
    unfinished_research: list[UnfinishedResearchView]


class InvestmentProposalPreviewView(BaseModel):
    """取得時刻付きの現在再試算。保存Proposal/termsとは独立したread-only結果。"""

    saved_proposal_id: str
    previewed_at: datetime
    status: Literal["unchanged", "changed", "blocked"]
    current_inputs: dict[str, Any] | None
    items: list[dict[str, Any]]
    change_reasons: list[GateReasonView]
    warnings: list[str] = Field(default_factory=list)
    portfolio_exposure: dict[str, Any] | None = None


type DeltaUnavailable = Literal[
    "screening_run",
    "previous_screening_run",
    "candidates",
    "candidate_estimate",
    "holdings",
    "market",
]


class CandidateEntryDeltaView(BaseModel):
    """A ticker whose presence in the 候補 changed between two runs.

    ``disclosed_since_previous`` is ``null`` when the store that holds disclosure
    dates could not answer, which must not read as "no disclosure".
    """

    ticker: str
    company_name: str | None
    er_annual_pct: float | None
    disclosed_since_previous: bool | None


class CandidateExpectedReturnDeltaView(BaseModel):
    """A ticker in both 候補s whose machine E[r] moved most."""

    ticker: str
    company_name: str | None
    er_annual_pct: float | None
    previous_er_annual_pct: float | None
    change_pp: float


class HoldingDeltaView(BaseModel):
    """Observed holding price movement; holding decisions belong to Investment Proposal."""

    ticker: str
    company_name: str | None
    change_since_previous_pct: float | None
    days_to_next_earnings: int | None


class DailyDeltaView(BaseModel):
    """What changed between the latest machine run and the one before it.

    The view compares observations, derived metrics, and machine estimates without
    turning them into causal claims or recommendations. It shows where to look; the
    decision to start new-stock or holding research stays human.

    ``unavailable`` lists the sections no store could answer, so an empty section is
    never read as "nothing changed". ``method_changed`` marks different screening or
    Candidate Discovery method hashes, so no 候補 deltas are reported. Missing
    method identity makes 候補 comparison unavailable. An E[r]-only model
    change keeps membership deltas but makes
    ``candidate_estimate`` unavailable and suppresses estimate movers.
    Missing E[r] on either side of a shared ticker also marks that section unavailable,
    while comparable tickers retain their movers.
    Holdings that cannot be compared are counts rather than rows: repeating the same
    list every day would bury the day's actual changes. ``er_moves_total`` says how
    many names cleared the threshold before the row cap, so a capped list does not
    hide its own remainder.
    """

    generated_at: datetime
    as_of: date | None
    previous_as_of: date | None
    method_changed: bool
    entered: list[CandidateEntryDeltaView]
    exited: list[CandidateEntryDeltaView]
    er_moves: list[CandidateExpectedReturnDeltaView]
    er_moves_total: int
    holdings: list[HoldingDeltaView]
    holdings_without_price: int
    unavailable: list[DeltaUnavailable]
