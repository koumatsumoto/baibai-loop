"""Move a task's event date onto the exchange's schedule once it is published.

A follow-up task is created the day a candidate is rejected, which is months
before the next announcement. The exchange's schedule reaches only weeks ahead, so
the date on the task starts as an estimate and stays one until the real date
enters that window. Nothing was watching for the moment it did, which left tasks
carrying a guess long after the answer existed.

This compares open tasks against the schedule and reports what would change. It
decides only; applying the change is the caller's step, so a wrong reading is
visible before it reaches the store.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from .models import Task

# The estimate a task starts with is a quarter-ahead guess, so a published date
# landing a few days either side of it is the same event and only sharpens it. A
# date far from the estimate is a different event — a schedule change, a ticker
# reusing a code, an estimate off by a quarter — and is reported rather than
# applied, because moving a task there silently would lose the disagreement.
ESTIMATE_TOLERANCE_DAYS = 45


@dataclass(frozen=True, slots=True, kw_only=True)
class EarningsReconciliation:
    """One task's standing against the published schedule."""

    task_id: str
    ticker: str
    current_event_date: date | None
    published_event_date: date
    outcome: str
    """`confirm` (already right), `update` (move it), or `disagree` (report only)."""

    @property
    def drift_days(self) -> int | None:
        if self.current_event_date is None:
            return None
        return (self.published_event_date - self.current_event_date).days


def reconcile_earnings_dates(
    tasks: Sequence[Task],
    published_by_ticker: Mapping[str, date],
    *,
    today: date,
) -> list[EarningsReconciliation]:
    """Compare open, dated, ticker-bearing tasks against the published schedule.

    A task whose ticker has no published date yet is absent from the result: the
    schedule not reaching that far is the normal state for most of a task's life,
    not a finding. Only tasks the schedule can speak to are reported.
    """
    results: list[EarningsReconciliation] = []
    for task in tasks:
        if task.status != "open" or task.ticker is None or task.kind == "ops":
            continue
        published = published_by_ticker.get(task.ticker)
        if published is None or published < today:
            continue
        if task.event_date == published:
            outcome = "confirm"
        elif (
            task.event_date is not None
            and abs((published - task.event_date).days) > ESTIMATE_TOLERANCE_DAYS
        ):
            outcome = "disagree"
        else:
            outcome = "update"
        results.append(
            EarningsReconciliation(
                task_id=task.task_id,
                ticker=task.ticker,
                current_event_date=task.event_date,
                published_event_date=published,
                outcome=outcome,
            )
        )
    return sorted(results, key=lambda item: (item.published_event_date, item.ticker))
