"""保有・運用成績・タスクの表示契約。"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


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
    earnings_calendar_available: bool | None = Field(
        default=None, description="保有決算予定の確認可否。保有なし/台帳不明はnull。"
    )
    ledger_error: str | None


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


class OutcomesView(BaseModel):
    outcomes: list[PortfolioOutcomeView]
