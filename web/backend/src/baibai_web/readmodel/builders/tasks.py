"""運用taskの一覧と関連情報を表示する。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta

from baibai_engine.foundation.time import JST
from baibai_engine.tasks.models import Task
from baibai_web.readmodel.builders.presentation import reservation_last_valid_day
from baibai_web.readmodel.models import (
    TasksView,
    TaskView,
    UpcomingEventView,
)
from baibai_web.sources.protocols import (
    LedgerSource,
    MarketSource,
    TaskSource,
)

_EVENT_WINDOW_DAYS = 14
_EVENT_KIND_ORDER = {"earnings": 0, "reservation_expiry": 1}


def build_tasks(
    tasks: TaskSource,
    ledger: LedgerSource,
    market: MarketSource,
) -> TasksView:
    """Build task workflow independently from Dashboard portfolio presentation."""

    now = datetime.now(JST)
    today = now.date()
    open_tasks, next_task = _task_views(tasks.list_tasks(), today=today)
    tasks_exist = tasks.exists()
    try:
        if not ledger.exists():
            return TasksView(
                generated_at=now,
                tasks_exist=tasks_exist,
                open_tasks=open_tasks,
                next_task=next_task,
                upcoming_events=[],
                ledger_error=None,
            )
        snapshot = ledger.snapshot()
    except (ValueError, RuntimeError, sqlite3.Error) as error:
        return TasksView(
            generated_at=now,
            tasks_exist=tasks_exist,
            open_tasks=open_tasks,
            next_task=next_task,
            upcoming_events=[],
            ledger_error=str(error),
        )

    security_names = market.security_names(as_of=today)
    holding_tickers = [holding.ticker for holding in snapshot.holdings]
    earnings_dates = market.next_earnings_dates(holding_tickers, as_of=today)
    return TasksView(
        generated_at=now,
        tasks_exist=tasks_exist,
        open_tasks=open_tasks,
        next_task=next_task,
        upcoming_events=_upcoming_events(
            today=today,
            earnings=earnings_dates,
            names=security_names,
            expiries=((item.ticker, item.expires_at) for item in snapshot.active_reservations),
        ),
        ledger_error=None,
    )


def _task_views(
    records: list[Task],
    *,
    today: date,
) -> tuple[list[TaskView], TaskView | None]:
    open_records = sorted(
        (item for item in records if item.status == "open"),
        key=lambda item: (item.due_date, item.task_id),
    )
    views = [_task_view(item, today=today) for item in open_records]
    return views, views[0] if views else None


def _task_view(record: Task, *, today: date) -> TaskView:
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


def _upcoming_events(
    *,
    today: date,
    earnings: Mapping[str, date],
    names: Mapping[str, str],
    expiries: Iterable[tuple[str, datetime | None]],
) -> list[UpcomingEventView]:
    """Collapse holding earnings and reservation expiries into one chronological list
    within the next ``_EVENT_WINDOW_DAYS`` days.

    The window is inclusive on both ends: an event dated today (days_until 0) through
    ``today + _EVENT_WINDOW_DAYS`` is surfaced; anything past or beyond is dropped.
    Reservation deadlines are exclusive instants, displayed on their last valid JST day.
    This calendar shows one milestone per ticker and day, not individual orders.
    """

    window_end = today + timedelta(days=_EVENT_WINDOW_DAYS)
    events: list[UpcomingEventView] = []
    for ticker, event_date in earnings.items():
        if today <= event_date <= window_end:
            events.append(
                UpcomingEventView(
                    event_date=event_date,
                    kind="earnings",
                    ticker=ticker,
                    label=names.get(ticker) or ticker,
                    days_until=(event_date - today).days,
                )
            )
    expiry_days = {
        (ticker, reservation_last_valid_day(expires_at))
        for ticker, expires_at in expiries
        if expires_at is not None
    }
    for ticker, event_date in expiry_days:
        if today <= event_date <= window_end:
            events.append(
                UpcomingEventView(
                    event_date=event_date,
                    kind="reservation_expiry",
                    ticker=ticker,
                    label=ticker,
                    days_until=(event_date - today).days,
                )
            )
    events.sort(key=lambda item: (item.event_date, _EVENT_KIND_ORDER[item.kind], item.ticker or ""))
    return events
