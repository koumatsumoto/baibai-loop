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
    latest_packet_path: str | None
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


class ResearchRevisionView(BaseModel):
    as_of: date
    packet_path: str
    recommendation: str
    confidence: str | None
    current_fair_value_yen: float | None
    model_version: str | None
    review_path: str | None


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


class SecurityDetailView(BaseModel):
    ticker: str
    company_name: str | None
    sector: str | None
    holding: HoldingView | None
    revisions: list[ResearchRevisionView]
    latest_packet: PacketDetailView | None
    candidate_row: CandidateRowView | None
    candidate_run: ScreeningRunView | None
