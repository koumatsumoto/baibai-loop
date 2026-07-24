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
    due_date: date
    event_label: str | None
    event_date: date | None
    overdue: bool


class UpcomingEventView(BaseModel):
    event_date: date
    kind: Literal["earnings", "reservation_expiry", "macro_valid_until"]
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
    source_path: str
    application_git_commit: str | None
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


class MachineSelectionView(BaseModel):
    selection_id: str
    run_revision_id: str
    profile: str
    macro_context_id: str | None
    created_at: datetime
    recommendations: list[dict[str, object]]
    longlist: list[dict[str, object]]


class ShortlistNarrativeView(BaseModel):
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
    sector_label: str | None = None


class ShortlistEntryView(BaseModel):
    ticker: str
    decision: str
    reason: str
    narrative: ShortlistNarrativeView | None = None


class ShortlistView(BaseModel):
    shortlist_id: str
    selection_id: str
    run_revision_id: str
    as_of: date
    published_at: datetime
    entries: list[ShortlistEntryView]


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


class MacroInvestmentConnectionView(BaseModel):
    summary: str
    sector_tilts: list[str]
    research_priority_hints: list[str]
    source_ids: list[str]


class MacroScenarioView(BaseModel):
    case: str
    direction: str
    summary: str
    conditions: list[str]
    investment_implications: list[str]
    source_ids: list[str]


class MacroMonitoringPointView(BaseModel):
    event: str
    condition: str
    view_change: str
    summary: str
    source_ids: list[str]


class MacroContextSectionView(BaseModel):
    section_id: str
    series: list[MacroSeriesReferenceView]
    fact_summary: list[MacroFactSummaryView]
    judgment: MacroSectionJudgmentView
    investment_connection: MacroInvestmentConnectionView
    change_since_previous: str | None
    material_deltas: list[MacroMaterialDeltaView]
    sizing_cautions: list[MacroSizingCautionView]
    scenarios: list[MacroScenarioView]
    monitoring_points: list[MacroMonitoringPointView]


class MacroContextView(BaseModel):
    context_id: str
    as_of: date
    valid_until: date
    published_at: datetime
    summary: str
    stale: bool
    sections: list[MacroContextSectionView]


class MacroContextRevisionView(BaseModel):
    context_id: str
    as_of: date
    valid_until: date
    published_at: datetime
    summary: str
    stale: bool


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
