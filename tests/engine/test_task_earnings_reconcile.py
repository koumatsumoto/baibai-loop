from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.jpx_models import JPXEarningsCalendarEntry, JPXEarningsCalendarSnapshot
from baibai_engine.storage.observations.ingest.jpx import store_jpx_earnings_calendar_snapshot
from baibai_engine.storage.observations.schema import open_connection
from baibai_engine.task_cli import main as task_main
from baibai_engine.tasks.earnings_reconcile import (
    reconcile_earnings_dates,
)
from baibai_engine.tasks.models import Task
from baibai_engine.tasks.service import TaskService

TODAY = date(2026, 7, 31)


def _task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "task_id": "task-20260731-4716",
        "title": "4716 を 1Q 後に再評価する",
        "kind": "follow-up",
        "status": "open",
        "ticker": "4716",
        "due_date": date(2026, 9, 25),
        "event_label": "4716 1Q決算",
        "event_date": date(2026, 9, 25),
        "created_at": TODAY,
    }
    values.update(overrides)
    return Task(**values)  # type: ignore[arg-type]


class ReconcileEarningsDatesTest(unittest.TestCase):
    def test_a_published_date_near_the_estimate_sharpens_it(self) -> None:
        results = reconcile_earnings_dates([_task()], {"4716": date(2026, 9, 18)}, today=TODAY)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].outcome, "update")
        self.assertEqual(results[0].drift_days, -7)

    def test_a_ticker_the_schedule_does_not_reach_is_not_reported(self) -> None:
        # Most of a task's life is spent beyond the schedule's window. Reporting
        # that as a finding would bury the tasks the schedule can actually speak to.
        results = reconcile_earnings_dates([_task()], {}, today=TODAY)

        self.assertEqual(results, [])

    def test_a_quarter_away_from_the_estimate_is_a_different_event(self) -> None:
        # Dates are hard-coded rather than derived from the constant: a test that
        # builds its fixture from the threshold passes for any threshold, so it
        # cannot detect the threshold being wrong. 91 days is the quarterly cadence.
        far = date(2026, 9, 25) + timedelta(days=91)
        results = reconcile_earnings_dates([_task()], {"4716": far}, today=TODAY)

        self.assertEqual(results[0].outcome, "disagree")

    def test_the_tolerance_boundary_sits_where_the_pre_registration_put_it(self) -> None:
        inside = reconcile_earnings_dates(
            [_task()], {"4716": date(2026, 9, 25) + timedelta(days=45)}, today=TODAY
        )
        outside = reconcile_earnings_dates(
            [_task()], {"4716": date(2026, 9, 25) + timedelta(days=46)}, today=TODAY
        )

        self.assertEqual(inside[0].outcome, "update")
        self.assertEqual(outside[0].outcome, "disagree")

    def test_an_announcement_today_is_still_the_next_event(self) -> None:
        # The live case: four tickers announce on the as-of itself. Excluding them
        # would drop exactly the tasks that are due.
        results = reconcile_earnings_dates(
            [_task(event_date=TODAY, due_date=TODAY)], {"4716": TODAY}, today=TODAY
        )

        self.assertEqual(results[0].outcome, "confirm")

    def test_an_ops_task_is_left_alone_even_with_a_ticker(self) -> None:
        results = reconcile_earnings_dates(
            [_task(kind="ops")], {"4716": date(2026, 9, 18)}, today=TODAY
        )

        self.assertEqual(results, [])

    def test_a_non_earnings_task_for_the_same_ticker_is_left_alone(self) -> None:
        tasks = [
            _task(),
            _task(
                task_id="task-20260731-4716-plan",
                title="4716 次期中計の資本配分を確認する",
                event_label="次期中計発表（推定日）",
                due_date=date(2027, 5, 14),
                event_date=date(2027, 5, 14),
            ),
        ]

        results = reconcile_earnings_dates(tasks, {"4716": date(2026, 9, 25)}, today=TODAY)

        self.assertEqual([result.task_id for result in results], ["task-20260731-4716"])

    def test_an_explicit_non_earnings_event_outweighs_earnings_context_in_the_title(self) -> None:
        results = reconcile_earnings_dates(
            [
                _task(
                    title="決算後に次期中計の資本配分を確認する",
                    event_label="次期中計発表（推定日）",
                    due_date=date(2027, 5, 14),
                    event_date=date(2027, 5, 14),
                )
            ],
            {"4716": date(2026, 9, 25)},
            today=TODAY,
        )

        self.assertEqual(results, [])

    def test_the_title_identifies_an_earnings_task_when_the_event_label_is_absent(self) -> None:
        results = reconcile_earnings_dates(
            [_task(title="4716 の決算を確認する", event_label=None)],
            {"4716": date(2026, 9, 25)},
            today=TODAY,
        )

        self.assertEqual(len(results), 1)

    def test_an_earnings_review_kind_does_not_require_a_text_marker(self) -> None:
        results = reconcile_earnings_dates(
            [_task(kind="earnings-review", title="4716 quarterly review", event_label=None)],
            {"4716": date(2026, 9, 25)},
            today=TODAY,
        )

        self.assertEqual(len(results), 1)

    def test_results_are_ordered_by_the_published_date(self) -> None:
        tasks = [
            _task(task_id="task-20260731-a", ticker="4716", event_date=date(2026, 9, 25)),
            _task(task_id="task-20260731-b", ticker="3608", event_date=date(2026, 9, 1)),
        ]
        published = {"4716": date(2026, 9, 18), "3608": date(2026, 9, 4)}
        results = reconcile_earnings_dates(tasks, published, today=TODAY)

        self.assertEqual([item.ticker for item in results], ["3608", "4716"])

    def test_a_date_that_already_matches_is_confirmed_without_a_change(self) -> None:
        results = reconcile_earnings_dates([_task()], {"4716": date(2026, 9, 25)}, today=TODAY)

        self.assertEqual(results[0].outcome, "confirm")

    def test_a_closed_task_is_left_alone(self) -> None:
        results = reconcile_earnings_dates(
            [_task(status="done", closed_at=TODAY)], {"4716": date(2026, 9, 18)}, today=TODAY
        )

        self.assertEqual(results, [])

    def test_a_task_without_a_ticker_is_left_alone(self) -> None:
        # The schedule is keyed by ticker, so a task without one is unreachable —
        # the guard has to hold even when a date for some other ticker is present.
        results = reconcile_earnings_dates(
            [_task(ticker=None, kind="other")], {"4716": date(2026, 9, 18)}, today=TODAY
        )

        self.assertEqual(results, [])

    def test_a_past_announcement_is_not_a_next_event(self) -> None:
        # The stored schedule keeps dates that have already happened; treating one
        # as the next event would move the task backwards.
        results = reconcile_earnings_dates([_task()], {"4716": date(2026, 6, 25)}, today=TODAY)

        self.assertEqual(results, [])

    def test_a_task_with_no_estimate_takes_the_published_date(self) -> None:
        results = reconcile_earnings_dates(
            [_task(event_date=None)], {"4716": date(2026, 9, 18)}, today=TODAY
        )

        self.assertEqual(results[0].outcome, "update")
        self.assertIsNone(results[0].drift_days)


def _store_schedule(path: Path, *, fetched_at: str = "2026-07-31T00:00:00Z") -> None:
    store_jpx_earnings_calendar_snapshot(
        path,
        JPXEarningsCalendarSnapshot(
            entries=(JPXEarningsCalendarEntry("4716", date(2026, 9, 18)),),
            source_urls=(),
            raw_record_count=1,
            excluded_record_count=0,
            superseded_record_count=0,
        ),
        fetched_at_utc=fetched_at,
    )


class TaskEarningsCliTest(unittest.TestCase):
    def test_unknown_or_stale_schedule_does_not_succeed_as_a_published_empty_schedule(self) -> None:
        for state in ("missing", "uncovered", "stale"):
            with self.subTest(state=state), TemporaryDirectory() as directory:
                path = Path(directory) / "market.sqlite"
                if state == "uncovered":
                    open_connection(path).close()
                elif state == "stale":
                    _store_schedule(path, fetched_at="2026-07-01T00:00:00Z")
                tasks = Path(directory) / "application.sqlite"
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    result = task_main(
                        ["--db", str(tasks), "reconcile-earnings", "--sqlite-path", str(path)],
                        today=TODAY,
                    )
                self.assertEqual(result, 1)
                self.assertIn("current earnings schedule unavailable", stderr.getvalue())
                self.assertFalse(tasks.exists())
                if state == "missing":
                    self.assertFalse(path.exists())

    def test_cli_reports_read_errors_without_creating_a_task_store(self) -> None:
        for state, message in (
            ("corrupt_file", "not a database"),
            ("partial_schema", "obsolete market SQLite schema"),
            ("corrupt_snapshot", "row count mismatch"),
        ):
            with self.subTest(state=state), TemporaryDirectory() as directory:
                path = Path(directory) / "market.sqlite"
                if state == "corrupt_file":
                    path.write_bytes(b"not a sqlite database")
                elif state == "partial_schema":
                    with closing(sqlite3.connect(path)) as connection, connection:
                        connection.execute("CREATE TABLE another_table(value TEXT)")
                else:
                    _store_schedule(path)
                    with closing(sqlite3.connect(path)) as connection, connection:
                        connection.execute("UPDATE source_coverage SET record_count = 2")
                before = path.read_bytes()
                tasks = Path(directory) / "application.sqlite"
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    result = task_main(
                        ["--db", str(tasks), "reconcile-earnings", "--sqlite-path", str(path)],
                        today=TODAY,
                    )
                self.assertEqual(result, 1)
                self.assertIn(message, stderr.getvalue())
                self.assertNotIn("schedule unavailable", stderr.getvalue())
                self.assertFalse(tasks.exists())
                self.assertEqual(path.read_bytes(), before)

    def test_verified_schedule_drives_reconciliation_without_changing_either_store(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "market.sqlite"
            _store_schedule(path)
            tasks = Path(directory) / "application.sqlite"
            task = TaskService(tasks).add(
                title="4716 を 1Q 後に再評価する",
                kind="follow-up",
                ticker="4716",
                due_date=date(2026, 9, 25),
                event_label="4716 1Q決算",
                event_date=date(2026, 9, 25),
                created_at=TODAY,
            )
            market_before, tasks_before = path.read_bytes(), tasks.read_bytes()
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = task_main(
                    ["--db", str(tasks), "reconcile-earnings", "--sqlite-path", str(path)],
                    today=TODAY,
                )
            self.assertEqual(result, 0)
            report, payload = stdout.getvalue().split("\n", 1)
            self.assertIn("reconcile-earnings\tupdate\t4716", report)
            self.assertEqual(
                safe_load(payload),
                {
                    "schedule_window_end": "2026-09-18",
                    "reconciliations": [
                        {
                            "task_id": task.task_id,
                            "ticker": "4716",
                            "current_event_date": "2026-09-25",
                            "published_event_date": "2026-09-18",
                            "drift_days": -7,
                            "outcome": "update",
                        }
                    ],
                },
            )
            self.assertEqual(path.read_bytes(), market_before)
            self.assertEqual(tasks.read_bytes(), tasks_before)


if __name__ == "__main__":
    unittest.main()
