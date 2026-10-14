"""選択と成果を既存正本へ照合する。connection/transactionはOperationが所有する。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from baibai_engine.position.ledger import replay_events_through
from baibai_engine.position.ledger_read import load_ledger_document
from baibai_engine.research.operation.progress import (
    HoldingProposal,
    ResearchProgress,
    ReviewedTarget,
    require_coverage,
)
from baibai_engine.research.position_review_service import PositionReviewService
from baibai_engine.research.selection import ResearchSelection, ResearchTarget, selection_targets
from baibai_engine.research.thesis_store import load_reviewed_thesis


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
    sqlite_path: Path,
    complete: bool,
    previous: ResearchProgress | None = None,
) -> ResearchProgress:
    require_coverage(selection_targets(selection), progress.outcomes, complete=complete)
    verified = []
    retained = {} if previous is None else {item.ticker: item for item in previous.outcomes}
    for outcome in progress.outcomes:
        if isinstance(outcome, (ReviewedTarget, HoldingProposal)):
            thesis_id = (
                outcome.thesis_id
                if isinstance(outcome, ReviewedTarget)
                else outcome.draft.thesis_id
            )
            pair = load_reviewed_thesis(connection, thesis_id)
            if (
                pair.document.input_snapshot.ticker != outcome.ticker
                or pair.core_sha256 != outcome.thesis_core_sha256
                or pair.review.review_id != outcome.thesis_review_id
            ):
                raise ValueError("research outcome differs from its Reviewed Thesis binding")
            row = connection.execute(
                "SELECT published_at FROM thesis WHERE thesis_id=?", (thesis_id,)
            ).fetchone()
            if row is None or datetime.fromisoformat(row[0]) > now:
                raise ValueError("research outcome precedes Thesis publication")
        if isinstance(outcome, HoldingProposal):
            if outcome.draft.ticker != outcome.ticker:
                raise ValueError("holding proposal ticker differs from the selected target")
            if retained.get(outcome.ticker) != outcome or outcome.checked_at is None:
                result = PositionReviewService(sqlite_path=sqlite_path).check_in_transaction(
                    connection, outcome.draft, now
                )
                if result.action != outcome.draft.action:
                    raise ValueError("holding proposal action differs from its validated draft")
                outcome = outcome.model_copy(update={"checked_at": now})
            elif outcome.checked_at > now:
                raise ValueError("completion precedes the checked holding proposal")
        verified.append(outcome)
    return ResearchProgress(outcomes=tuple(verified))
