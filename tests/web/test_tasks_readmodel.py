from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from baibai_engine.read_api import PortfolioLedgerError
from baibai_web.readmodel.builders import build_tasks
from baibai_web.readmodel.builders.tasks import _upcoming_events
from baibai_web.sources.types import TaskRecord


@pytest.mark.parametrize(
    ("expires_at", "last_valid_day"),
    [
        (None, None),
        ("2026-10-17T00:00:00+09:00", date(2026, 10, 16)),
        ("2026-10-16T15:00:00+00:00", date(2026, 10, 16)),
        ("2026-10-16T16:00:00+00:00", date(2026, 10, 17)),
    ],
)
def test_exclusive_order_expiry_is_shown_on_last_valid_jst_day(
    expires_at: str | None, last_valid_day: date | None
) -> None:
    expiry = datetime.fromisoformat(expires_at) if expires_at is not None else None
    events = _upcoming_events(
        today=date(2026, 10, 14), earnings={}, names={}, expiries=[("2222", expiry)]
    )
    assert [event.event_date for event in events] == (
        [] if last_valid_day is None else [last_valid_day]
    )


def test_tasks_remain_visible_when_ledger_cannot_build_upcoming_events(mocker) -> None:
    tasks = mocker.Mock()
    tasks.exists.return_value = True
    tasks.list_tasks.return_value = [
        TaskRecord(
            task_id="task-later",
            title="Later task",
            kind="earnings-review",
            status="open",
            ticker="2331",
            due_date=date(2099, 1, 2),
            event_label=None,
            event_date=None,
            body_md=None,
            related_refs=(),
            created_at=date(2026, 8, 31),
            closed_at=None,
        ),
        TaskRecord(
            task_id="task-next",
            title="Next task",
            kind="follow-up",
            status="open",
            ticker=None,
            due_date=date(2099, 1, 1),
            event_label=None,
            event_date=None,
            body_md=None,
            related_refs=(),
            created_at=date(2026, 8, 30),
            closed_at=None,
        ),
    ]
    ledger = mocker.Mock()
    ledger.exists.return_value = True
    ledger.snapshot.side_effect = PortfolioLedgerError("invalid ledger")
    research = mocker.Mock()
    candidates = mocker.Mock()
    market = mocker.Mock()

    view = build_tasks(tasks, ledger, research, candidates, market)

    assert [task.task_id for task in view.open_tasks] == ["task-next", "task-later"]
    assert view.next_task is not None
    assert view.next_task.task_id == "task-next"
    assert view.upcoming_events == []
    assert view.ledger_error == "invalid ledger"
    research.revisions.assert_not_called()
    candidates.latest_run.assert_not_called()
    market.next_earnings_dates.assert_not_called()


def test_tasks_treat_absent_ledger_as_normal(mocker) -> None:
    tasks = mocker.Mock()
    tasks.exists.return_value = False
    tasks.list_tasks.return_value = []
    ledger = mocker.Mock()
    ledger.exists.return_value = False

    view = build_tasks(tasks, ledger, mocker.Mock(), mocker.Mock(), mocker.Mock())

    assert view.open_tasks == []
    assert view.upcoming_events == []
    assert view.ledger_error is None


def test_task_events_need_dates_and_names_not_holding_valuation(mocker) -> None:
    tasks, ledger, research, screening, market = [mocker.Mock() for _ in range(5)]
    tasks.exists.return_value = True
    tasks.list_tasks.return_value = []
    ledger.exists.return_value = True
    holding = mocker.Mock(spec_set=["ticker"], ticker="2331")
    ledger.snapshot.return_value.holdings = [holding]
    ledger.snapshot.return_value.active_reservations = []
    research.revisions.return_value = []
    screening.latest_run.return_value = None
    now = datetime(2026, 10, 14, 12, tzinfo=ZoneInfo("Asia/Tokyo"))
    mocker.patch("baibai_web.readmodel.builders.tasks.datetime").now.return_value = now
    today = now.date()
    market.next_earnings_dates.return_value = {"2331": today}
    market.price_comparison_basis_is_confirmed.side_effect = AssertionError("unneeded valuation")

    view = build_tasks(tasks, ledger, research, screening, market)

    assert [(event.kind, event.ticker) for event in view.upcoming_events] == [("earnings", "2331")]
    market.price_comparison_basis_is_confirmed.assert_not_called()


def test_event_window_order_and_labels_are_preserved() -> None:
    events = _upcoming_events(
        today=date(2026, 10, 14),
        earnings={
            "1001": date(2026, 10, 13),
            "1002": date(2026, 10, 14),
            "1003": date(2026, 10, 28),
            "1004": date(2026, 10, 29),
        },
        names={"1002": "Company"},
        expiries=[("2001", datetime(2026, 10, 15, tzinfo=ZoneInfo("Asia/Tokyo")))],
    )
    assert [(event.kind, event.ticker, event.label, event.days_until) for event in events] == [
        ("earnings", "1002", "Company", 0),
        ("reservation_expiry", "2001", "2001", 0),
        ("earnings", "1003", "1003", 14),
    ]
