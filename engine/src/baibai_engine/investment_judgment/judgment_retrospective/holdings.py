"""Connect a recorded holding to its last formal review, never to a standing order."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from functools import cache

from baibai_engine.investment_judgment.contracts import _datetime
from baibai_engine.investment_judgment.judgment_retrospective.coverage import (
    item_review_notes,
    original_item_questions,
)
from baibai_engine.investment_judgment.judgment_retrospective.document import ProposalItemRef
from baibai_engine.investment_judgment.trade_proposal.query import (
    is_erroneous_proposal_as_of,
    load_investment_proposal,
)
from baibai_engine.portfolio.ledger.events import (
    ExecutionEvent,
    LedgerEvent,
    PortfolioLedgerDocument,
    holding_episodes,
)
from baibai_engine.portfolio.ledger.read import load_ledger_events


def _openings(events: tuple[LedgerEvent, ...], cutoff: datetime) -> dict[str, str]:
    return {
        episode.first_buy.ticker: episode.first_buy.event_id
        for episode in holding_episodes(tuple(e for e in events if e.occurred_at <= cutoff))
        if not episode.closed
    }


def holding_review_history(
    conn: sqlite3.Connection,
    ledger: PortfolioLedgerDocument,
    tickers: set[str],
    *,
    as_of: date,
    known_at: datetime,
) -> dict[str, object]:
    pending = set(tickers)
    rows: dict[str, dict[str, object]] = {}
    current = _openings(ledger.events, known_at) if pending else {}
    # Ledger order between same-instant fills is stable. An earlier buy reported
    # late can move the start without ending the position the author reviewed.
    fill_order = {
        event.event_id: i
        for i, event in enumerate(ledger.events)
        if pending and isinstance(event, ExecutionEvent) and event.occurred_at <= known_at
    }

    @cache
    def previous(head: int, checked: datetime) -> dict[str, str] | None:
        try:
            return _openings(load_ledger_events(conn, append_head=head), checked)
        except ValueError:
            # Lost evidence about the position must not promote a review to this episode.
            return None

    headers: list[tuple[datetime, str]] = []
    if pending:
        for identity, day, stamp in conn.execute(
            "SELECT proposal_id,as_of,published_at FROM investment_proposal "
            "WHERE request_scope='review_positions'"
        ):
            try:
                published = _datetime(stamp)
                if published <= known_at and date.fromisoformat(day) <= as_of:
                    headers.append((published, identity))
            except ValueError:
                # Its time cannot be ordered against any other decision for these tickers.
                for ticker in _mentioned(conn, identity) & pending:
                    rows[ticker] = {"status": "unreadable_publication"}
                    pending.remove(ticker)

    for published, identity in sorted(headers, reverse=True):
        if not pending:
            break
        matched = pending & _mentioned(conn, identity)
        if not matched:
            continue
        try:
            if is_erroneous_proposal_as_of(conn, identity, known_at):
                continue
            proposal = load_investment_proposal(conn, identity)
        except (ValueError, FileNotFoundError):
            for ticker in matched:
                rows[ticker] = {"status": "unreadable_publication"}
            pending -= matched
            continue
        inputs = proposal.inputs
        original = previous(inputs.ledger_head, inputs.checked_at)
        for index, item in enumerate(proposal.items):
            ticker = item.ticker
            if ticker not in matched:
                continue
            ref = ProposalItemRef(
                proposal_id=identity, draft_sha256=proposal.review.draft_sha256, ticker=ticker
            )
            row: dict[str, object] = {
                "origin": ref.model_dump(mode="json"),
                "item_index": index,
                "published_at": published.isoformat(),
                "original_inputs_checked_at": inputs.checked_at.isoformat(),
                "original_ledger_as_of": inputs.ledger_as_of.isoformat(),
            }
            if (
                not original
                or ticker not in original
                or ticker not in current
                or original[ticker] not in fill_order
                or not any(p.ticker == ticker and p.quantity > 0 for p in inputs.positions)
            ):
                row["status"] = "holding_episode_unconfirmed"
            elif fill_order[original[ticker]] < fill_order[current[ticker]]:
                row["status"] = "different_recorded_holding_episode"
            else:
                status, notes = item_review_notes(conn, ref, published, known_at)
                row.update(
                    status="same_recorded_holding_episode",
                    **original_item_questions(item),
                    follow_up_coverage=status,
                    # Use coverage/get for every comparison and its exact evidence, not a
                    # synthesis declaring which condition is now fulfilled or still pending.
                    follow_up_notes=[
                        {key: n[key] for key in ("reference", "recorded_at", "lesson")}
                        for n in notes
                        if not n["superseded_at_cutoff"]
                    ],
                )
                if item.action == "sell":
                    row["original_sell_reason"] = item.reason_code
                elif item.action == "hold":
                    row["original_holding_basis"] = item.holding_basis
            rows[ticker] = row
        pending -= matched

    rows.update({ticker: {"status": "not_recorded"} for ticker in pending})
    return {
        "role": "previous_formal_questions_not_current_holdings_or_orders",
        "current_ledger_as_of": ledger.as_of.isoformat(),
        "fact_cutoff": known_at.isoformat(),
        "items": dict(sorted(rows.items())),
    }


def _mentioned(conn: sqlite3.Connection, identity: str) -> set[str]:
    # Do not hide a newer decision behind a missing item index. The small target
    # header and the item index identify whom to verify; neither is evidence by itself.
    return {
        ticker
        for (ticker,) in conn.execute(
            "SELECT ticker FROM investment_proposal_item WHERE proposal_id=? UNION "
            "SELECT CASE WHEN j.type='object' THEN json_extract(j.value,'$.ticker') END "
            "FROM investment_proposal p, json_each(p.payload,'$.research_scope.targets') j "
            "WHERE p.proposal_id=?",
            (identity, identity),
        )
        if isinstance(ticker, str)
    }
