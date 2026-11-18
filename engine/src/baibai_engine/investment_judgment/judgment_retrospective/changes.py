"""Route newly saved company documents to earlier proposal questions, not to conclusions."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from functools import cache
from pathlib import Path

from baibai_engine.investment_judgment.company_research.store import load_research
from baibai_engine.investment_judgment.contracts import _datetime
from baibai_engine.investment_judgment.judgment_retrospective.context import assessment_reference
from baibai_engine.investment_judgment.judgment_retrospective.coverage import (
    original_item_questions,
)
from baibai_engine.investment_judgment.stock_assessment.store import load_reviewed_assessment
from baibai_engine.investment_judgment.trade_proposal.contracts import TradeProposal
from baibai_engine.investment_judgment.trade_proposal.query import (
    is_erroneous_proposal_as_of,
    load_investment_proposal,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only


def _reference(connection: sqlite3.Connection, kind: str, identity: str) -> dict[str, object]:
    if kind == "company_research":
        research = load_research(connection, identity)
        return {
            "reference": research.reference.model_dump(mode="json"),
            "information_as_of": research.document.as_of.isoformat(),
        }
    pair = load_reviewed_assessment(connection, identity)
    return {
        "reference": assessment_reference(pair).model_dump(mode="json"),
        "information_as_of": pair.document.input_snapshot.as_of.isoformat(),
    }


def review_changes(
    *, after: datetime, known_at: datetime, db_path: Path | None = None
) -> dict[str, object]:
    after, cutoff = _datetime(after), _datetime(known_at)
    if after >= cutoff:
        raise ValueError("after must precede known_at")
    events: list[dict[str, object]] = []
    unavailable: set[tuple[str, str, str]] = set()
    with closing(connect_read_only(db_path)) as conn:
        conn.execute("BEGIN")
        # Read narrow headers, not every old proposal, company snapshot or valuation.
        # Writers can retain different UTC offsets; SQL string or julianday comparisons
        # would not give exact, replayable boundaries.
        receipts = conn.execute(
            "WITH subjects AS (SELECT DISTINCT ticker FROM investment_proposal_item) "
            "SELECT 'company_research', research_id, ticker, recorded_at FROM company_research "
            "WHERE ticker IN subjects UNION ALL "
            "SELECT 'reviewed_assessment', assessment_id, ticker, published_at "
            "FROM stock_assessment WHERE ticker IN subjects"
        )
        in_window = []
        for kind, identity, ticker, stamp in receipts:
            try:
                received = _datetime(stamp)
            except ValueError:
                unavailable.add((kind, identity, "receipt_time_unreadable"))
                continue
            if after < received <= cutoff:
                in_window.append((received, kind, identity, ticker))

        @cache
        def origins(ticker: str) -> list[tuple[datetime, str, int]]:
            result = []
            for proposal_id, stamp, index in conn.execute(
                "SELECT p.proposal_id,p.published_at,i.item_index FROM investment_proposal_item i "
                "JOIN investment_proposal p USING(proposal_id) WHERE i.ticker=?",
                (ticker,),
            ):
                try:
                    result.append((_datetime(stamp), proposal_id, index))
                except ValueError:
                    unavailable.add(("proposal", proposal_id, "publication_time_unreadable"))
            return sorted(result)

        @cache
        def read_origin(proposal_id: str) -> tuple[TradeProposal, bool]:
            proposal = load_investment_proposal(conn, proposal_id)
            return proposal, is_erroneous_proposal_as_of(conn, proposal_id, cutoff)

        for received, kind, identity, ticker in sorted(in_window):
            earlier = [row for row in origins(ticker) if row[0] < received]
            if not earlier:
                continue
            try:
                source = _reference(conn, kind, identity)
            except (ValueError, FileNotFoundError):
                unavailable.add((kind, identity, "document_unreadable"))
                continue
            items = []
            for published, proposal_id, index in earlier:
                try:
                    proposal, erroneous = read_origin(proposal_id)
                except (ValueError, FileNotFoundError):
                    unavailable.add(
                        ("proposal", proposal_id, "publication_or_correction_unreadable")
                    )
                    continue
                items.append(
                    {
                        "proposal_id": proposal_id,
                        "draft_sha256": proposal.review.draft_sha256,
                        "published_at": published.isoformat(),
                        "item_index": index,
                        "proposal_status": "corrected_as_erroneous" if erroneous else "published",
                        **original_item_questions(proposal.items[index]),
                    }
                )
            events.append(
                {
                    "kind": kind,
                    "ticker": ticker,
                    "received_at": received.isoformat(),
                    **source,
                    "earlier_proposal_items": items,
                }
            )
    return {
        "after_exclusive": after.isoformat(),
        "known_at_inclusive": cutoff.isoformat(),
        "scope": "newly_saved_same_issuer_documents_for_prior_proposal_items",
        "events": events,
        "unreadable": [
            {"kind": kind, "id": identity, "reason": reason}
            for kind, identity, reason in sorted(unavailable)
        ],
        "next_after": None if unavailable else cutoff.isoformat(),
    }
