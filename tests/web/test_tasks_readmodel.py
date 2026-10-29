from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.machine_analysis.observations.disclosures.jpx_models import JPXProviderError
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.portfolio.ledger.events import PortfolioLedgerError
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_engine.tasks.models import Task
from baibai_web.readmodel.builders.tasks import _upcoming_events, build_tasks
from baibai_web.sources import db_sources
from baibai_web.sources.db_sources import DbLedgerSource


@pytest.mark.parametrize(
    ("expires_at", "last_valid_day"),
    [
        (None, None),
        ("2026-10-17T00:00:00+09:00", date(2026, 10, 16)),
        ("2026-10-17T00:00:00.000001+09:00", date(2026, 10, 17)),
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
        Task(
            task_id="task-20260831-later",
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
        Task(
            task_id="task-20260830-next",
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
    ledger.facts_snapshot.side_effect = PortfolioLedgerError("invalid ledger")
    market = mocker.Mock()

    view = build_tasks(tasks, ledger, market)

    assert [task.task_id for task in view.open_tasks] == [
        "task-20260830-next",
        "task-20260831-later",
    ]
    assert view.next_task is not None
    assert view.next_task.task_id == "task-20260830-next"
    assert view.upcoming_events == []
    assert view.ledger_error == "invalid ledger"
    market.next_earnings_dates.assert_not_called()


def test_tasks_treat_absent_ledger_as_normal(mocker) -> None:
    tasks = mocker.Mock()
    tasks.exists.return_value = False
    tasks.list_tasks.return_value = []
    ledger = mocker.Mock()
    ledger.exists.return_value = False

    view = build_tasks(tasks, ledger, mocker.Mock())

    assert view.open_tasks == []
    assert view.upcoming_events == []
    assert view.ledger_error is None


def test_task_events_need_dates_and_names_not_holding_valuation(mocker) -> None:
    tasks, ledger, market = [mocker.Mock() for _ in range(3)]
    tasks.exists.return_value = True
    tasks.list_tasks.return_value = []
    ledger.exists.return_value = True
    holding = mocker.Mock(spec_set=["ticker"], ticker="2331")
    ledger.facts_snapshot.return_value.holdings = [holding]
    ledger.facts_snapshot.return_value.active_reservations = []
    market.security_names.return_value = {}
    now = datetime(2026, 10, 14, 12, tzinfo=ZoneInfo("Asia/Tokyo"))
    mocker.patch("baibai_web.readmodel.builders.tasks.datetime").now.return_value = now
    today = now.date()
    market.next_earnings_dates.return_value = {"2331": today}
    ledger.snapshot.side_effect = AssertionError("unneeded valuation")

    view = build_tasks(tasks, ledger, market)

    assert [(event.kind, event.ticker) for event in view.upcoming_events] == [("earnings", "2331")]
    ledger.snapshot.assert_not_called()


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
    assert [(event.kind, event.ticker, event.label, event.event_date) for event in events] == [
        ("earnings", "1002", "Company", date(2026, 10, 14)),
        ("reservation_expiry", "2001", "2001", date(2026, 10, 14)),
        ("earnings", "1003", "1003", date(2026, 10, 28)),
    ]


def test_calendar_collapses_same_ticker_expiries_without_collapsing_other_milestones() -> None:
    first = datetime.fromisoformat("2026-10-17T00:00:00+09:00")
    same_day = datetime.fromisoformat("2026-10-16T15:00:00+00:00")
    next_day = datetime.fromisoformat("2026-10-18T00:00:00+09:00")
    events = _upcoming_events(
        today=date(2026, 10, 14),
        earnings={"2222": date(2026, 10, 16)},
        names={},
        expiries=[
            ("2222", first),
            ("2222", first),
            ("2222", same_day),
            ("2222", next_day),
            ("3333", first),
            ("4444", None),
        ],
    )
    assert [(event.kind, event.ticker, event.event_date) for event in events] == [
        ("earnings", "2222", date(2026, 10, 16)),
        ("reservation_expiry", "2222", date(2026, 10, 16)),
        ("reservation_expiry", "3333", date(2026, 10, 16)),
        ("reservation_expiry", "2222", date(2026, 10, 17)),
    ]


@pytest.mark.parametrize("calendar", [None, {}])
def test_unavailable_earnings_remains_distinct_from_confirmed_empty(mocker, calendar):
    tasks, ledger, market = [mocker.Mock() for _ in range(3)]
    tasks.exists.return_value = True
    tasks.list_tasks.return_value = []
    ledger.exists.return_value = True
    ledger.facts_snapshot.return_value.holdings = [mocker.Mock(ticker="2331")]
    ledger.facts_snapshot.return_value.active_reservations = [
        mocker.Mock(ticker="2331", expires_at=datetime.fromisoformat("2026-10-17T00:00:00+09:00"))
    ]
    market.security_names.return_value = {}
    market.next_earnings_dates.return_value = calendar
    mocker.patch(
        "baibai_web.readmodel.builders.tasks.datetime"
    ).now.return_value = datetime.fromisoformat("2026-10-14T12:00:00+09:00")
    view = build_tasks(tasks, ledger, market)
    assert view.earnings_calendar_available is (calendar is not None)
    assert view.earnings_calendar_error is None
    assert [(event.kind, event.event_date) for event in view.upcoming_events] == [
        ("reservation_expiry", date(2026, 10, 16))
    ]


def test_db_tasks_compose_ledger_facts_without_reading_market_valuation(tmp_path, mocker):
    app = tmp_path / "app.sqlite"
    market_path = tmp_path / "absent-market.sqlite"
    seed_ledger(
        app, load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    )
    before = app.read_bytes()
    now = datetime.fromisoformat("2026-07-20T09:00:00+09:00")
    mocker.patch("baibai_web.readmodel.builders.tasks.datetime").now.return_value = now
    mocker.patch.object(db_sources, "datetime").now.return_value = now
    valuation = mocker.patch.object(
        db_sources, "current_portfolio", side_effect=AssertionError("unneeded valuation")
    )
    quote = mocker.patch(
        "baibai_engine.portfolio.holdings.valuation.read_unadjusted_close",
        side_effect=AssertionError("unneeded quote"),
    )
    basis = mocker.patch(
        "baibai_engine.portfolio.holdings.valuation.market_basis_is_confirmed",
        side_effect=AssertionError("unneeded price basis"),
    )
    tasks, market = mocker.Mock(), mocker.Mock()
    tasks.exists.return_value = True
    tasks.list_tasks.return_value = []
    market.security_names.return_value = {"2331": "保有企業"}
    market.next_earnings_dates.return_value = {"2331": date(2026, 7, 21)}

    view = build_tasks(tasks, DbLedgerSource(app, market_path), market)

    assert view.ledger_error is None
    assert [(event.kind, event.ticker, event.event_date) for event in view.upcoming_events] == [
        ("earnings", "2331", date(2026, 7, 21)),
        ("reservation_expiry", "8929", date(2026, 7, 31)),
    ]
    market.next_earnings_dates.assert_called_once_with(["2331"], as_of=now.date())
    valuation.assert_not_called()
    quote.assert_not_called()
    basis.assert_not_called()
    assert app.read_bytes() == before
    assert not market_path.exists()


@pytest.fixture
def task_event_sources(mocker):
    tasks, ledger, market = [mocker.Mock() for _ in range(3)]
    tasks.exists.return_value = True
    tasks.list_tasks.return_value = [
        Task(
            task_id="task-20261014-next",
            title="確認する用件",
            kind="follow-up",
            status="open",
            due_date=date(2026, 10, 15),
            created_at=date(2026, 10, 14),
        )
    ]
    ledger.exists.return_value = True
    ledger.facts_snapshot.return_value.holdings = [mocker.Mock(ticker="2331")]
    ledger.facts_snapshot.return_value.active_reservations = [
        mocker.Mock(ticker="8929", expires_at=datetime.fromisoformat("2026-10-17T00:00:00+09:00"))
    ]
    market.next_earnings_dates.return_value = {"2331": date(2026, 10, 15)}
    market.security_names.return_value = {"2331": "保有企業"}
    mocker.patch(
        "baibai_web.readmodel.builders.tasks.datetime"
    ).now.return_value = datetime.fromisoformat("2026-10-14T12:00:00+09:00")
    return tasks, ledger, market


@pytest.mark.parametrize("error_type", [JPXProviderError, SQLiteSchemaError, sqlite3.DatabaseError])
def test_calendar_read_error_preserves_tasks_and_reservation_deadlines(
    task_event_sources, error_type
):
    tasks, ledger, market = task_event_sources
    market.next_earnings_dates.side_effect = error_type("calendar read failed")

    view = build_tasks(tasks, ledger, market)

    assert [task.task_id for task in view.open_tasks] == ["task-20261014-next"]
    assert view.next_task == view.open_tasks[0]
    assert [(event.kind, event.ticker, event.event_date) for event in view.upcoming_events] == [
        ("reservation_expiry", "8929", date(2026, 10, 16))
    ]
    assert view.earnings_calendar_available is False
    assert view.earnings_calendar_error == "calendar read failed"
    assert view.ledger_error is None
    market.security_names.assert_not_called()


def test_no_holdings_skips_all_market_reads_and_preserves_reservation_deadlines(task_event_sources):
    tasks, ledger, market = task_event_sources
    ledger.facts_snapshot.return_value.holdings = []

    view = build_tasks(tasks, ledger, market)

    assert len(view.open_tasks) == 1
    assert [(event.kind, event.ticker) for event in view.upcoming_events] == [
        ("reservation_expiry", "8929")
    ]
    assert view.earnings_calendar_available is None
    assert view.earnings_calendar_error is None
    market.next_earnings_dates.assert_not_called()
    market.security_names.assert_not_called()


def test_unreadable_names_keep_earnings_with_ticker_labels(task_event_sources, caplog):
    tasks, ledger, market = task_event_sources
    market.security_names.side_effect = JQuantsProviderError("invalid master")

    view = build_tasks(tasks, ledger, market)

    assert [(event.kind, event.label) for event in view.upcoming_events] == [
        ("earnings", "2331"),
        ("reservation_expiry", "8929"),
    ]
    assert view.earnings_calendar_available is True
    assert view.earnings_calendar_error is None
    assert "invalid master" in caplog.text


@pytest.mark.parametrize("method", ["next_earnings_dates", "security_names"])
@pytest.mark.parametrize("error_type", [TypeError, ValueError, RuntimeError])
def test_unexpected_market_errors_are_not_hidden(task_event_sources, method, error_type):
    tasks, ledger, market = task_event_sources
    getattr(market, method).side_effect = error_type("unexpected implementation error")

    with pytest.raises(error_type, match="unexpected implementation error"):
        build_tasks(tasks, ledger, market)
