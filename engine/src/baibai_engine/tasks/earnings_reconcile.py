"""Say where a task's event date stands against the exchange's published schedule.

A follow-up task is created the day a candidate is rejected, months before the
next announcement. When the schedule already reaches that far the task takes the
published date; when it does not, a human picks the trigger date. So a task whose
date came from the schedule keeps agreeing with it, and the ones that would move
are the ones a human chose — which is why this reports and never writes. The task
規約 (ops-maintenance skill) keeps that write boundary with the human; `task edit`
applies what this names.
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
_EARNINGS_MARKERS = ("決算", "earnings")


def _is_earnings_task(task: Task) -> bool:
    """Return whether the task's trigger is an earnings announcement.

    ``follow-up`` also covers shareholder meetings and medium-term plans, so the
    ticker alone cannot bind such a task to the exchange earnings calendar. The
    explicit earnings-review kind or the human-facing trigger text supplies that
    missing event identity without widening the task schema.
    """
    if task.kind == "ops":
        return False
    if task.kind == "earnings-review":
        return True
    # An explicit event label identifies the trigger. The title may mention an
    # earnings announcement only as background (for example, "決算後に中計確認"),
    # so it is a fallback rather than a second, equally authoritative signal.
    trigger_text = task.event_label if task.event_label is not None else task.title
    trigger_text = trigger_text.casefold()
    return any(marker in trigger_text for marker in _EARNINGS_MARKERS)


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
    """Compare open, ticker-bearing earnings tasks against the published schedule.

    A task whose ticker has no published date yet is absent from the result: the
    schedule not reaching that far is the normal state for most of a task's life,
    not a finding. Only tasks the schedule can speak to are reported.
    """
    results: list[EarningsReconciliation] = []
    for task in tasks:
        if task.status != "open" or task.ticker is None or not _is_earnings_task(task):
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
