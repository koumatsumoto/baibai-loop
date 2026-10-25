"""選択と評価成果を正本へ照合する。transactionはOperationが所有する。"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from baibai_engine.investment_judgment.operation.progress import ResearchProgress, require_coverage
from baibai_engine.investment_judgment.selection import (
    ResearchSelectionRecord as ResearchSelection,
    ResearchTarget,
    selection_targets,
)
from baibai_engine.investment_judgment.stock_assessment_store import require_assessment_ref
from baibai_engine.position.ledger import replay_events_through
from baibai_engine.position.ledger_read import load_ledger_document


def validate_selection(
    connection: sqlite3.Connection, selection: ResearchSelection, *, now: datetime
) -> None:
    validate_target_purposes(connection, selection_targets(selection), now=now)


def validate_target_purposes(
    connection: sqlite3.Connection, targets: tuple[ResearchTarget, ...], *, now: datetime
) -> None:
    ledger = load_ledger_document(connection)
    held: set[str] = set()
    if ledger is not None:
        if any(event.occurred_at > now for event in ledger.events):
            raise ValueError("selection instant omits reported ledger facts")
        snapshot = replay_events_through(ledger.events, now)
        held = {
            ticker
            for ticker, lots in snapshot.lots.items()
            if any(lot.quantity > 0 for lot in lots)
        }
    for target in targets:
        if (target.ticker in held) != (target.purpose == "holding"):
            raise ValueError("purpose_conflict: target purpose must agree with the current holding")


def validate_progress(
    connection: sqlite3.Connection,
    selection: ResearchSelection,
    progress: ResearchProgress,
    *,
    now: datetime,
    complete: bool,
    previous: ResearchProgress | None = None,
) -> ResearchProgress:
    if now.utcoffset() is None:
        raise ValueError("research progress instant must include a UTC offset")
    require_coverage(selection_targets(selection), progress.outcomes, complete=complete)
    if previous is not None:
        retained = {(item.ticker, item.purpose) for item in previous.outcomes}
        incoming = {(item.ticker, item.purpose) for item in progress.outcomes}
        if not retained <= incoming:
            raise ValueError("recorded research outcomes cannot be silently removed")
        if previous.proposal_ref is not None and progress.proposal_ref is None:
            raise ValueError("recorded Proposal reference cannot be silently removed")
    for outcome in progress.outcomes:
        if outcome.status != "reviewed":
            continue
        pair = require_assessment_ref(connection, outcome.assessment_ref)
        if pair.document.input_snapshot.ticker != outcome.ticker:
            raise ValueError("research outcome differs from its Reviewed Assessment ticker")
        row = connection.execute(
            "SELECT published_at FROM stock_assessment WHERE assessment_id=?",
            (outcome.assessment_ref.assessment_id,),
        ).fetchone()
        if row is None or datetime.fromisoformat(row[0]) > now:
            raise ValueError("research outcome precedes Assessment publication")
    return progress
