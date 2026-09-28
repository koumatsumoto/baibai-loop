"""運用taskの一覧と関連情報を表示する。"""

from __future__ import annotations

from datetime import date, datetime

from baibai_engine.read_api import PortfolioLedgerError
from baibai_web.readmodel.builders.presentation import (
    _JST,
    holding_view,
    latest_research_by_ticker,
    security_names_for_run,
    upcoming_events,
)
from baibai_web.readmodel.models import (
    ReservationView,
    TasksView,
    TaskView,
)
from baibai_web.sources.protocols import (
    LedgerSource,
    MarketPriceSource,
    ResearchSource,
    ScreeningSource,
    TaskSource,
)
from baibai_web.sources.types import TaskRecord


def build_tasks(
    tasks: TaskSource,
    ledger: LedgerSource,
    research: ResearchSource,
    screening: ScreeningSource,
    market: MarketPriceSource,
) -> TasksView:
    """Build task workflow independently from Dashboard portfolio presentation."""

    now = datetime.now(_JST)
    today = now.date()
    open_tasks, next_task = _task_views(tasks.list_tasks(), today=today)
    tasks_exist = tasks.exists()
    if not ledger.exists():
        return TasksView(
            generated_at=now,
            tasks_exist=tasks_exist,
            open_tasks=open_tasks,
            next_task=next_task,
            upcoming_events=[],
            ledger_error=None,
        )
    try:
        snapshot = ledger.snapshot()
    except PortfolioLedgerError as error:
        return TasksView(
            generated_at=now,
            tasks_exist=tasks_exist,
            open_tasks=open_tasks,
            next_task=next_task,
            upcoming_events=[],
            ledger_error=str(error),
        )

    latest_research = latest_research_by_ticker(research.revisions())
    security_names = security_names_for_run(screening.latest_run())
    holding_tickers = [holding.ticker for holding in snapshot.holdings]
    earnings_dates = market.next_earnings_dates(holding_tickers, as_of=today)
    holdings = [
        holding_view(
            holding,
            revision=latest_research.get(holding.ticker),
            security_name=security_names.get(holding.ticker),
            next_earnings_date=earnings_dates.get(holding.ticker),
        )
        for holding in snapshot.holdings
    ]
    reservations = [
        ReservationView(
            reservation_id=item.reservation_id,
            ticker=item.ticker,
            sector=item.sector,
            remaining_quantity=item.remaining_quantity,
            price_guard_yen=str(item.price_guard_yen),
            reserved_yen=item.reserved_yen,
            expires_at=item.expires_at,
        )
        for item in snapshot.active_reservations
    ]
    return TasksView(
        generated_at=now,
        tasks_exist=tasks_exist,
        open_tasks=open_tasks,
        next_task=next_task,
        upcoming_events=upcoming_events(
            today=today,
            holdings=holdings,
            reservations=reservations,
        ),
        ledger_error=None,
    )


def _task_views(
    records: list[TaskRecord],
    *,
    today: date,
) -> tuple[list[TaskView], TaskView | None]:
    open_records = sorted(
        (item for item in records if item.status == "open"),
        key=lambda item: (item.due_date, item.task_id),
    )
    views = [_task_view(item, today=today) for item in open_records]
    return views, views[0] if views else None


def _task_view(record: TaskRecord, *, today: date) -> TaskView:
    return TaskView(
        task_id=record.task_id,
        title=record.title,
        kind=record.kind,
        status=record.status,
        ticker=record.ticker,
        due_date=record.due_date,
        event_label=record.event_label,
        event_date=record.event_date,
        overdue=record.due_date < today,
    )
