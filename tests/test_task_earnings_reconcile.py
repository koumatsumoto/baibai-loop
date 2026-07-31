from __future__ import annotations

import unittest
from datetime import date, timedelta

from baibai_engine.tasks.earnings_reconcile import (
    ESTIMATE_TOLERANCE_DAYS,
    reconcile_earnings_dates,
)
from baibai_engine.tasks.models import Task

TODAY = date(2026, 7, 31)


def _task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "task_id": "task-20260731-4716",
        "title": "4716 を 1Q 後に再評価する",
        "kind": "follow-up",
        "status": "open",
        "ticker": "4716",
        "due_date": date(2026, 9, 25),
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

    def test_a_date_far_from_the_estimate_is_reported_rather_than_applied(self) -> None:
        # A published date a quarter away from the estimate is a different event,
        # not a sharper reading of the same one. Moving the task there silently
        # would lose the disagreement.
        far = date(2026, 9, 25) + timedelta(days=ESTIMATE_TOLERANCE_DAYS + 1)
        results = reconcile_earnings_dates([_task()], {"4716": far}, today=TODAY)

        self.assertEqual(results[0].outcome, "disagree")

    def test_a_date_that_already_matches_is_confirmed_without_a_change(self) -> None:
        results = reconcile_earnings_dates([_task()], {"4716": date(2026, 9, 25)}, today=TODAY)

        self.assertEqual(results[0].outcome, "confirm")

    def test_a_closed_task_is_left_alone(self) -> None:
        results = reconcile_earnings_dates(
            [_task(status="done", closed_at=TODAY)], {"4716": date(2026, 9, 18)}, today=TODAY
        )

        self.assertEqual(results, [])

    def test_a_task_without_a_ticker_is_left_alone(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
