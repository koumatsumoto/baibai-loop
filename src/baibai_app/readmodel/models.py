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


type SystemStoreName = Literal["market", "runs", "macro", "baibai"]


class SystemStoreView(BaseModel):
    """One machine store's depth and freshness.

    ``row_count`` and ``latest_date`` come from the store's representative table,
    so a retention accident or a feed that stopped landing shows up as a number
    that moved even when the view it feeds still renders.
    """

    store: SystemStoreName
    exists: bool
    size_bytes: int | None
    row_count: int | None
    latest_date: date | None
    updated_at: datetime | None


class SystemProviderView(BaseModel):
    """A series whose most recent acquisition attempt failed, and for how long."""

    series_id: str
    name: str
    consecutive_failures: int
    failing_since: str
    last_error: str | None


class SystemView(BaseModel):
    """Operational state of the pipeline, exported as views/system.json.

    Deliberately carries no judgment input: everything here is about whether the
    machinery ran, never about what a number means for a holding or a candidate.
    """

    generated_at: datetime
    batch: MetaBatch | None
    stores: list[SystemStoreView]
    failing_providers: list[SystemProviderView]
    # Registered series with no acquisition attempt on record. A streak needs
    # rows to count, so without this a never-tried series reads as healthy.
    never_attempted_series: list[str]
    provider_series_total: int


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
    due_date: date
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
    next_event: TaskView | None
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


class ProposalView(BaseModel):
    proposal_id: str
    ticker: str
    thesis_id: str
    review_id: str
    created_at: datetime
    status: str
    decided_at: datetime | None
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
    proposals: list[ProposalView]
    outcomes: list[PortfolioOutcomeView]


class ScreeningRunView(BaseModel):
    run_id: str
    run_date: date
    asof_date: date
    run_at: datetime
    universe_size: int
    candidate_count: int
    # The revision the selections in the same payload bind to; ``run_id`` is the
    # public identifier a reader sees.
    run_revision_id: str
    stale: bool


type PortfolioState = Literal["unheld", "held", "reserved", "held_and_reserved"]


class CandidateRowView(BaseModel):
    ticker: str
    name: str | None
    sector_33: str | None
    market_cap_oku: float | None
    avg_turnover_oku: float | None
    per_trailing: float | None
    per_forward: float | None
    pbr: float | None
    ev_ebitda: float | None
    p_s: float | None
    pcfr: float | None
    dividend_yield: float | None
    er_annual: float | None
    er_reversion_annual: float | None
    er_carry_annual: float | None
    bargain_score: float | None
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
    data_quality_flags: list[str]
    portfolio_state: PortfolioState
    has_research: bool
    # FV アンカーは machine selection の longlist だけが持つので、longlist へ入らな
    # かった候補では空になる。read-only app は FV を導出しない。
    fair_value_anchor_yen: float | None = None
    fair_value_gap_pct: float | None = None


class ScreeningView(BaseModel):
    run: ScreeningRunView | None
    rows: list[CandidateRowView]
    selections: list[MachineSelectionView]
    shortlists: list[ShortlistView]


class ScreeningHistoryView(BaseModel):
    dates: list[date]


class ScreeningHistoryRunView(BaseModel):
    """Stable UI projection of one retained screening run."""

    run: ScreeningRunView
    rows: list[CandidateRowView]


class SelectionLonglistEntryView(BaseModel):
    """機械 rank 上位の候補 1 件。FV アンカーと E[r] はここだけが持つ。"""

    rank: int | None
    ticker: str
    name: str | None
    market_price_yen: float | None
    fair_value_anchor_yen: float | None
    fair_value_gap_pct: float | None
    expected_return_pct: float | None
    screening_playbook: str | None
    liquidity_status: str | None
    selection_reasons: list[str]
    durability_warnings: list[str]
    event_warnings: list[str]


class MachineSelectionView(BaseModel):
    selection_id: str
    run_revision_id: str
    profile: str
    macro_context_id: str | None
    created_at: datetime
    longlist: list[SelectionLonglistEntryView]


class ShortlistNarrativeView(BaseModel):
    """発行済み shortlist の判断。read 側は欠けた field を空欄として通す。

    必須性を強制するのは publish の write path だけであり、read model が同じ必須を
    課すと、schema を進めた瞬間に旧 revision を読む export と API が落ちる。
    """

    ploss: str
    why: str
    temporary: str
    structural: str
    survive: str
    unlock: str
    counter: str
    research: str
    value: str
    prov: str
    upside: str | None = None
    downside: str | None = None
    rr: str | None = None
    catalyst: str | None = None
    catalyst_date: date | None = None
    macro: str | None = None
    sector_label: str | None = None


class ShortlistEntryView(BaseModel):
    ticker: str
    decision: str
    reason: str
    rank: int | None = None
    narrative: ShortlistNarrativeView | None = None


class ShortlistView(BaseModel):
    shortlist_id: str
    selection_id: str
    run_revision_id: str
    as_of: date
    published_at: datetime
    entries: list[ShortlistEntryView]
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


class HoldingReviewView(BaseModel):
    holding_review_id: str
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
    holding_reviews: list[HoldingReviewView]
    candidate_row: CandidateRowView | None
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


class MacroTriggerResultView(BaseModel):
    point_index: int
    event: str
    condition_index: int
    series_id: str
    comparison: str
    threshold: float
    status: str
    observed_at: date | None
    value: float | None
    view_change: str


class MacroTriggerEvaluationView(BaseModel):
    """Whether the report's own invalidation conditions have been met since it was written."""

    asof: date
    evaluated: int
    fired: int
    results: list[MacroTriggerResultView]


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
    # Absent when no indicator store could answer, which is a normal state for a
    # checkout that only carries the application database.
    triggers: MacroTriggerEvaluationView | None = None


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
    """Macro overview: the report index (summaries) plus the indicator panel.

    Full report sections are served per revision by ``MacroContextView`` at
    ``/api/macro/context/{context_id}`` so the overview stays a lightweight index.
    """

    as_of: date
    period: Literal["1y", "5y", "10y", "max"]
    granularity: Literal["daily", "weekly", "monthly", "yearly"]
    reports: list[MacroContextRevisionView]
    groups: list[MacroGroupView]
