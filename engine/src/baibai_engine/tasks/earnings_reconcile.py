"""Say where a task's event date stands against the exchange's published schedule.

A follow-up task is created the day a candidate is rejected, months before the
next announcement. When the schedule already reaches that far the task takes the
published date; when it does not, a human picks the trigger date. So a task whose
date came from the schedule keeps agreeing with it, and the ones that would move
are the ones a human chose — which is why this reports and never writes.
the task 規約 (ops-maintenance skill) keeps that write boundary with the human; `task edit` applies
what this names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from .models import Task

# A hand-picked date is a quarter-ahead guess, so a published date landing a few
# days either side of it is the same event read more sharply. A date far from it is
# a different event — a schedule change, an estimate off by a quarter — and is
# named as a disagreement rather than a refinement, because the two call for
# different actions from the reader.
ESTIMATE_TOLERANCE_DAYS = 45


@dataclass(frozen=True, slots=True, kw_only=True)
class EarningsReconciliation:
    """One task's standing against the published schedule."""

    task_id: str
    ticker: str
    current_event_date: date | None
    published_event_date: date
    outcome: str
    """`confirm` (matches), `update` (the schedule is sharper), `disagree` (differs)."""

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
