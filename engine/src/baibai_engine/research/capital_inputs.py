"""新規配分工程でReviewed Thesisと現在の資本・quoteを共通の購入条件へ渡し、配分入力を産む。"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from baibai_engine.position.ledger import (
    ExecutionEvent,
    replay_events_through,
)
from baibai_engine.position.ledger_read import load_ledger_in_transaction
from baibai_engine.position.market_source import (
    UnadjustedCloseObservation,
    quantity_basis_is_confirmed,
    read_unadjusted_close,
)
from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.research.capital_allocation import AllocationAlternative
from baibai_engine.research.entry_policy import EntryResult, evaluate_entry
from baibai_engine.research.thesis_store import (
    ReviewedThesis,
    latest_thesis_id,
    load_reviewed_thesis,
)


def evaluate_allocation_in_transaction(
    connection: sqlite3.Connection,
    *,
    alternative: AllocationAlternative,
    assessment_id: str,
    as_of: date,
    now: datetime,
    sqlite_path: Path,
    budget_max_yen: int,
) -> tuple[ReviewedThesis, EntryResult, UnadjustedCloseObservation | None, int]:
    """Assemble real current inputs for the shared CAA/Planning entry rule."""
    pair = load_reviewed_thesis(connection, alternative.thesis_id)
    if (
        pair.document.input_snapshot.ticker != alternative.ticker
        or pair.core_sha256 != alternative.thesis_core_sha256
        or pair.review.review_id != alternative.thesis_review_id
    ):
        raise ValueError("allocated Reviewed Thesis binding differs")
    document, _ = load_ledger_in_transaction(connection)
    if document.as_of > now or any(event.occurred_at > now for event in document.events):
        raise ValueError("ledger contains facts after the judgment instant")
    state = replay_events_through(document.events, now)
    quote = read_unadjusted_close(sqlite_path=sqlite_path, ticker=alternative.ticker, at=now)
    original_price = next(
        (
            fact
            for fact in pair.document.input_snapshot.facts
            if fact.fact_id == pair.document.valuation.market_price_fact_id
        ),
        None,
    )
    basis_confirmed = (
        quote is not None
        and original_price is not None
        and (
            quantity_basis_is_confirmed(
                sqlite_path=sqlite_path,
                ticker=alternative.ticker,
                from_date=original_price.as_of,
                through_date=quote.price_as_of,
            )
        )
    )
    result = evaluate_entry(
        pair.document,
        reviewed=True,
        latest=latest_thesis_id(connection, alternative.ticker) == pair.thesis_id,
        as_of=as_of,
        price_yen=None if quote is None else Decimal(str(quote.close_yen)),
        price_as_of=None if quote is None else quote.price_as_of,
        basis_confirmed=basis_confirmed,
        minimum_required_annual_return_pct=Decimal(
            str(PORTFOLIO_POLICY["valuation"]["minimum_required_annual_return_pct"])
        ),
        market_price_max_age_days=PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"],
        held_quantity=sum(lot.quantity for lot in state.lots.get(alternative.ticker, [])),
        active_reservation=any(
            item.ticker == alternative.ticker for item in state.active_reservations.values()
        ),
        assessment_executed=any(
            isinstance(event, ExecutionEvent)
            and event.side == "buy"
            and event.decision_reference == assessment_id
            for event in document.events
        ),
        available_cash_yen=Decimal(state.available_cash_yen),
        budget_max_yen=Decimal(budget_max_yen),
        board_lot=PORTFOLIO_POLICY["order_constraints"]["board_lot"],
    )
    return pair, result, quote, state.available_cash_yen
