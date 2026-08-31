from __future__ import annotations

from datetime import date

from baibai_engine.read_api import PortfolioLedgerError
from baibai_web.readmodel.builders import build_tasks
from baibai_web.sources.types import TaskRecord


def test_tasks_remain_visible_when_ledger_cannot_build_upcoming_events(mocker) -> None:
    tasks = mocker.Mock()
    tasks.exists.return_value = True
    tasks.list_tasks.return_value = [
        TaskRecord(
            task_id="task-later",
            title="Later task",
            kind="research",
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
            kind="review",
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
