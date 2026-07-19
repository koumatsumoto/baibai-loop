"""JSON DTOs exposed only to the bundled read-only UI."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


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
    latest_packet_id: str | None
    recommendation: str | None


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


class DashboardView(BaseModel):
    generated_at: datetime
    ledger_exists: bool
    ledger_error: str | None
    ledger_as_of: datetime | None
    ledger_stale: bool
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
    open_tasks: list[TaskView]
    next_task: TaskView | None
    next_event: TaskView | None
    tasks_exist: bool
    research_load_errors: list[str]


class ScreeningRunView(BaseModel):
    run_id: str
    run_date: date
    asof_date: date
    universe_size: int
    candidate_count: int
    source_path: str


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
    net_cash_to_market_cap: float | None
    fcf_yield: float | None
    ocf_yield: float | None
    equity_ratio: float | None
    price_change_20d: float | None
    gap_from_52w_low: float | None
    next_earnings_date: str | None
    held: bool
    has_research: bool


class ScreeningView(BaseModel):
    run: ScreeningRunView | None
    rows: list[CandidateRowView]
    runs: list[ScreeningPublicationView]
    selections: list[MachineSelectionView]
    reviewed_shortlists: list[ReviewedShortlistView]


class ScreeningPublicationView(BaseModel):
    run_revision_id: str
    run_id: str
    asof_date: date
    run_at: datetime
    candidate_count: int


class MachineSelectionView(BaseModel):
    selection_id: str
    run_revision_id: str
    profile: str
    macro_context_id: str | None
    created_at: datetime
    recommendations: list[dict[str, object]]
    audit_pool: list[dict[str, object]]


class ReviewedShortlistEntryView(BaseModel):
    ticker: str
    decision: str
    reason: str


class ReviewedShortlistView(BaseModel):
    shortlist_id: str
    selection_id: str
    run_revision_id: str
    published_at: datetime
    entries: list[ReviewedShortlistEntryView]


class ResearchRevisionView(BaseModel):
    as_of: date
    packet_id: str
    recommendation: str
    confidence: str | None
    current_fair_value_yen: float | None
    model_version: str | None
    review_id: str | None


class ScenarioView(BaseModel):
    name: str
    horizon_years: int


class PacketDetailView(BaseModel):
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
    packet_id: str
    candidate_packet_id: str | None
    action: str
    note: str | None


class SecurityDetailView(BaseModel):
    ticker: str
    company_name: str | None
    sector: str | None
    holding: HoldingView | None
    revisions: list[ResearchRevisionView]
    latest_packet: PacketDetailView | None
    holding_reviews: list[HoldingReviewView]
    candidate_row: CandidateRowView | None
    candidate_run: ScreeningRunView | None


class MacroMaterialDeltaView(BaseModel):
    channel: str
    direction: str
    materiality: str
    summary: str
    used_for: str


class MacroSizingCautionView(BaseModel):
    severity: str
    summary: str


class MacroContextView(BaseModel):
    context_id: str
    as_of: date
    valid_until: date
    published_at: datetime
    summary: str
    stale: bool
    material_deltas: list[MacroMaterialDeltaView]
    sizing_cautions: list[MacroSizingCautionView]
    research_questions: list[str]
    refresh_triggers: list[str]
    changes_since_previous: list[str]


class MacroContextRevisionView(BaseModel):
    context_id: str
    as_of: date
    valid_until: date
    published_at: datetime
    summary: str


class MacroPointView(BaseModel):
    observed_at: date
    value: float


class MacroSeriesView(BaseModel):
    series_id: str
    label: str
    name: str
    unit: str
    points: list[MacroPointView]


class MacroGroupView(BaseModel):
    title: str
    series: list[MacroSeriesView]


class MacroView(BaseModel):
    as_of: date
    context: MacroContextView | None
    context_history: list[MacroContextRevisionView]
    groups: list[MacroGroupView]
