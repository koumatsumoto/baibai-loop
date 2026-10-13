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
    market_price_as_of: datetime | None
    market_value_yen: int | None
    unrealized_pnl_yen: int | None
    unrealized_pnl_pct: float | None
    pmax_raw_yen: float | None
    pmax_gap_pct: float | None
    latest_thesis_id: str | None
    disposition: str | None
    next_earnings_date: str | None = None


class ReservationView(BaseModel):
    reservation_id: str
    side: Literal["buy", "sell"]
    ticker: str
    sector: str
    remaining_quantity: int
    price_guard_yen: str
    reserved_yen: int
    expires_at: datetime
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
    valuation_as_of: datetime | None
    valuation_stale: bool
    total_capital_yen: int | None
    available_cash_yen: int | None
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
    research_load_errors: list[str]


class TasksView(BaseModel):
    """Task/event workflow projection, separate from portfolio presentation."""

    generated_at: datetime
    tasks_exist: bool
    open_tasks: list[TaskView]
    next_task: TaskView | None
    upcoming_events: list[UpcomingEventView]
    ledger_error: str | None


class OperationResearchTargetView(BaseModel):
    ticker: str
    purpose: Literal["new", "holding"]
    outcome_status: Literal["reviewed", "proposal", "unfinished"] | None
    thesis_id: str | None
    thesis_review_id: str | None
    unfinished_reason: str | None
    references: list[str]
    checked_at: datetime | None
    holding_proposal: dict[str, object] | None


class OperationResearchView(BaseModel):
    request: str
    research_triage_id: str | None
    targets: list[OperationResearchTargetView]
    capital_allocation_assessment_id: str | None
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
    portfolio_twr_pct: float | None = None
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
    # 保存済みSecurity Analysisの機械anchor。Review Set入りを条件にせず再計算もしない。
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
    as_of_start: date
    as_of_end: date
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


class ExternalAssessmentSecurityView(BaseModel):
    ticker: str
    name: str | None
    sector_33: str | None
    portfolio_state: PortfolioState
    has_research: bool


class ScreeningView(BaseModel):
    run: ScreeningRunView | None
    security_analyses: list[SecurityAnalysisRowView]
    external_assessment_securities: list[ExternalAssessmentSecurityView] = Field(
        default_factory=list
    )
    external_assessment_cutoff: datetime | None = None
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


class ReviewSetNominationView(BaseModel):
    valuation_approach_id: str
    valuation_method_id: str
    rank: int


class ReviewSetIdentityLiquidityView(BaseModel):
    market_cap_oku: float | None
    avg_turnover_oku: float | None
    listing_span_days: float | None
    jpx_flags: list[str] | None


class ReviewSetValuationView(BaseModel):
    per_forward: float | None
    per_trailing: float | None
    pbr: float | None
    ev_ebitda: float | None
    p_s: float | None
    pcfr: float | None


class ReviewSetCurrentEarningsView(BaseModel):
    fcf_yield: float | None
    ocf_yield: float | None
    forecast_special_gain_flag: bool | None
    forecast_full_year_loss_flag: bool | None


class ReviewSetNormalizedEarningsView(BaseModel):
    normalized_per_3fy: float | None
    normalized_per_3fy_sector_gap: float | None


class ReviewSetAssetValueView(BaseModel):
    asset_backed_ratio: float | None
    net_cash_to_market_cap: float | None
    investment_securities: float | None
    equity_ratio: float | None


class ReviewSetReinvestmentView(BaseModel):
    p_s_sector_gap: float | None
    operating_return_on_capital_proxy: float | None
    sales_yoy: float | None
    operating_margin: float | None
    fcf_yield: float | None


class ReviewSetExpectedReturnView(BaseModel):
    er_annual: float | None
    er_reversion_annual: float | None
    er_carry_annual: float | None
    fv_sector_median_yen: float | None
    fv_self_range_yen: float | None
    er_origin: str | None
    er_model_version: str | None
    er_unit: str | None
    er_assumptions: str | None
    dividend_yield: float | None = None
    dividend_basis: str | None = None
    net_share_change_yoy: float | None = None
    carry_missing_assumption: str | None = None


class ReviewSetDataQualityView(BaseModel):
    bs_carry_forward_fields: str | None
    bs_carry_forward_lag_days: float | None
    edinet_failure_reasons: str | None
    stale_fin_flag: bool | None
    ttm_quality_ev_ebitda: Literal["exact", "approximated", "unavailable"] | None = None
    ttm_quality_fcf: Literal["exact", "approximated", "unavailable"] | None = None


class ReviewSetContextView(BaseModel):
    next_earnings_status: str | None
    next_earnings_estimated_date: str | None
    margin_short_to_adv: float | None
    tse_capital_policy_status: str | None
    large_holding_filing_within_lookback: bool | None
    tender_offer_filing_within_lookback: bool | None


class ReviewSetAnalysisView(BaseModel):
    identity_liquidity: ReviewSetIdentityLiquidityView
    valuation: ReviewSetValuationView
    current_earnings: ReviewSetCurrentEarningsView
    normalized_earnings: ReviewSetNormalizedEarningsView
    asset_value: ReviewSetAssetValueView
    reinvestment: ReviewSetReinvestmentView | None
    expected_return: ReviewSetExpectedReturnView
    data_quality: ReviewSetDataQualityView
    context: ReviewSetContextView


class ReviewSetEntryView(BaseModel):
    """One member of the exact approach Nomination union."""

    ticker: str
    name: str | None
    sector_33: str | None
    nominations: list[ReviewSetNominationView]
    analysis: ReviewSetAnalysisView


class ReviewSetView(BaseModel):
    review_set_id: str
    run_revision_id: str
    created_at: datetime
    entries: list[ReviewSetEntryView]


class ReviewSetEntrySnapshotView(BaseModel):
    """Machine coordinates frozen into one Research Triage judgment entry."""

    name: str | None = None
    sector_33: str | None = None
    nominations: list[dict[str, object]] | None
    expected_return: dict[str, object] | None
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
    review_set_entry_snapshot: ReviewSetEntrySnapshotView | None = None


class ResearchTriageView(BaseModel):
    research_triage_id: str
    review_set_id: str
    run_revision_id: str
    as_of: date
    published_at: datetime
    entries: list[ResearchTriageEntryView]


class ResearchTriageDetailView(ResearchTriageView):
    payload: dict[str, object]


class ResearchRevisionView(BaseModel):
    as_of: date
    thesis_id: str
    disposition: str
    pmax_raw_yen: float | None
    review_id: str | None
    status: str


class ThesisDetailView(BaseModel):
    revision: ResearchRevisionView
    projection: dict[str, object]


class PositionReviewView(BaseModel):
    position_review_id: str
    as_of: date
    thesis_id: str
    action: str | None
    note: str | None


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
    price_basis: Literal["adjusted_close_preferred"]
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
    latest_thesis: ThesisDetailView | None
    position_reviews: list[PositionReviewView]
    security_analysis: SecurityAnalysisRowView | None
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


class AllocationAlternativeView(BaseModel):
    ticker: str
    disposition: str
    rationale: str
    thesis_id: str
    thesis_review_id: str | None
    case_status: str | None
    base_annualized_return_pct: float | None
    pmax_raw_yen: float | None
    valuation_as_of: str | None
    thesis_projection: dict[str, Any] = Field(default_factory=dict)
    thesis_review: dict[str, Any] | None = None


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
    decision: str
    headline: str
    research_triage_id: str | None
    alternative_count: int
    allocated_ticker: str | None
    research_operation_id: str | None = None


class CapitalAllocationAssessmentsView(BaseModel):
    assessments: list[CapitalAllocationAssessmentSummaryView]


class UnfinishedResearchView(BaseModel):
    ticker: str
    reason: str
    references: list[str]


class CapitalAllocationAssessmentView(BaseModel):
    capital_allocation_assessment_id: str
    as_of: date
    published_at: datetime
    decision: str
    headline: str
    research_triage_id: str | None
    market_context_assessment_id: str | None
    comparison: str
    foregone_alternatives: str
    research_operation_id: str | None = None
    selected_new_target_count: int | None = None
    unfinished_research: list[UnfinishedResearchView] = Field(default_factory=list)
    alternatives: list[AllocationAlternativeView]
    content_review: AssessmentReviewView


type DeltaUnavailable = Literal[
    "screening_run",
    "previous_screening_run",
    "review_set",
    "review_set_estimate",
    "holdings",
    "market",
]


class ReviewSetEntryDeltaView(BaseModel):
    """A ticker whose presence in the Review Set changed between two runs.

    ``disclosed_since_previous`` is ``null`` when the store that holds disclosure
    dates could not answer, which must not read as "no disclosure".
    """

    ticker: str
    company_name: str | None
    er_annual_pct: float | None
    disclosed_since_previous: bool | None


class ReviewSetExpectedReturnDeltaView(BaseModel):
    """A ticker in both Review Sets whose machine E[r] moved most."""

    ticker: str
    company_name: str | None
    er_annual_pct: float | None
    previous_er_annual_pct: float | None
    change_pp: float


class HoldingDeltaView(BaseModel):
    """Observed holding price movement; holding decisions belong to Position Review."""

    ticker: str
    company_name: str | None
    change_since_previous_pct: float | None
    days_to_next_earnings: int | None


class DailyDeltaView(BaseModel):
    """What changed between the latest machine run and the one before it.

    Every field is an observation or a comparison of observations. The view names no
    cause and carries no recommendation: it tells the reader where to look, and the
    decision to start a research cycle or a Position Review stays human.

    ``unavailable`` lists the sections no store could answer, so an empty section is
    never read as "nothing changed". ``method_changed`` marks different screening or
    Candidate Discovery method hashes, so no Review Set deltas are reported. Missing
    method identity makes Review Set comparison unavailable. An E[r]-only model
    change keeps membership deltas but makes
    ``review_set_estimate`` unavailable and suppresses estimate movers.
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
    entered: list[ReviewSetEntryDeltaView]
    exited: list[ReviewSetEntryDeltaView]
    er_moves: list[ReviewSetExpectedReturnDeltaView]
    er_moves_total: int
    holdings: list[HoldingDeltaView]
    holdings_without_price: int
    unavailable: list[DeltaUnavailable]
