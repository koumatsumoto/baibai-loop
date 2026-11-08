"""選択と評価成果を正本へ照合する。transactionはOperationが所有する。"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from baibai_engine.investment_judgment.company_research.query import company_context
from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.market_assessment.query import (
    latest_assessment_from_connection,
)
from baibai_engine.investment_judgment.stock_assessment.store import require_assessment_ref
from baibai_engine.investment_judgment.workflow.operation.progress import (
    ResearchProgress,
    require_coverage,
)
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionV4,
    ResearchTarget,
    selection_targets,
)
from baibai_engine.investment_judgment.workflow.selection import (
    ResearchSelectionRecord as ResearchSelection,
)
from baibai_engine.portfolio.ledger.events import replay_events_through
from baibai_engine.portfolio.ledger.read import load_ledger_document
from baibai_engine.primitives.json import canonical_json


def validate_selection(
    connection: sqlite3.Connection, selection: ResearchSelection, *, now: datetime
) -> None:
    validate_target_purposes(connection, selection_targets(selection), now=now)
    if isinstance(selection, GeneratedResearchSelectionV4):
        market = latest_assessment_from_connection(
            connection, as_of=selection.research_as_of, known_at=selection.input_known_at
        )
        market_projection = (
            None
            if market is None
            else assessment_projection(market, as_of=selection.research_as_of)
        )
        if canonical_json(selection.inputs.saved_market_context_assessment) != canonical_json(
            market_projection
        ):
            raise ValueError("saved Market Assessment differs from the selected cutoff")
        expected = {
            target.ticker: company_context(
                connection,
                target.ticker,
                as_of=selection.research_as_of,
                known_at=selection.input_known_at,
            )
            for target in selection.targets
        }
        if canonical_json(
            {
                ticker: context.model_dump(mode="json")
                for ticker, context in selection.inputs.saved_company_context.items()
            }
        ) != canonical_json(
            {ticker: context.model_dump(mode="json") for ticker, context in expected.items()}
        ):
            raise ValueError("saved company context differs from the selected cutoff")


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
        if pair.published_at > now:
            raise ValueError("research outcome precedes Assessment publication")
    return progress
