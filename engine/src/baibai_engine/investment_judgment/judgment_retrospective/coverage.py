"""Start the follow-up from every item actually published, not the notes already written."""

from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path

from baibai_engine.investment_judgment.contracts import _datetime
from baibai_engine.investment_judgment.judgment_retrospective.document import (
    ProposalItemRef,
    RetrospectiveRecord,
)
from baibai_engine.investment_judgment.judgment_retrospective.store import (
    RetrospectiveReadError,
    load_retrospective,
    validate_correction,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import ProposalItem, order_readiness
from baibai_engine.investment_judgment.trade_proposal.query import (
    is_erroneous_proposal_as_of,
    load_investment_proposal,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only


def original_item_questions(item: ProposalItem) -> dict[str, object]:
    return {
        "ticker": item.ticker,
        "original_action": item.action,
        "original_rationale": item.rationale,
        "original_review_conditions": list(item.review_conditions),
        **(
            {
                "original_economic_decision": item.economic_decision,
                "original_order_readiness": order_readiness(item),
                "original_session_limit": (
                    item.terms.model_dump(
                        mode="json", include={"limit_price_yen", "target_session"}
                    )
                    if item.terms is not None
                    else None
                ),
            }
            if item.action == "buy"
            else {}
        ),
        **(
            {
                "original_blockers": [b.model_dump(mode="json") for b in item.blockers],
                "original_resume_conditions": list(item.resume_conditions),
            }
            if item.action == "defer"
            else {}
        ),
    }


def item_review_notes(
    connection: sqlite3.Connection, origin: ProposalItemRef, published: datetime, cutoff: datetime
) -> tuple[str, list[dict[str, object]]]:
    records: list[RetrospectiveRecord] = []
    unreadable = False
    for identity, stamp in connection.execute(
        "SELECT retrospective_id,recorded_at FROM judgment_retrospective WHERE ticker=?",
        (origin.ticker,),
    ):
        try:
            # Ignore future receipts before opening their bodies, including later corrections.
            if not published <= _datetime(stamp) <= cutoff:
                continue
            record = load_retrospective(connection, identity)
            source = record.document.origin
            if not isinstance(source, ProposalItemRef) or source.proposal_id != origin.proposal_id:
                continue
            if source != origin:
                raise RetrospectiveReadError("retrospective refers to a different proposal digest")
            validate_correction(connection, record.document, recorded_at=record.recorded_at)
        except ValueError:
            # A record we cannot attribute is not proof that this issuer had no follow-up.
            unreadable = True
            continue
        records.append(record)
    records.sort(key=lambda r: (r.recorded_at, r.reference.retrospective_id))
    superseded = {r.document.corrects_ref for r in records if r.document.corrects_ref is not None}
    notes: list[dict[str, object]] = []
    results: list[str] = []
    for record in records:
        doc = record.document
        old = record.reference in superseded
        if not old:
            results.extend(comparison.result for comparison in doc.comparisons)
        notes.append(
            {
                "reference": record.reference.model_dump(mode="json"),
                "recorded_at": record.recorded_at.isoformat(),
                "as_of": doc.as_of.isoformat(),
                "milestone": doc.milestone,
                "superseded_at_cutoff": old,
                "corrects_ref": None if doc.corrects_ref is None else doc.corrects_ref.model_dump(),
                "authored_comparisons": [c.model_dump(mode="json") for c in doc.comparisons],
                "lesson": None if doc.lesson is None else doc.lesson.model_dump(),
            }
        )
    if unreadable:
        return "unreadable", notes
    if not records:
        return "not_recorded", notes
    if not results:
        return "lesson_only", notes
    if all(result == "unresolved" for result in results):
        return "unresolved_comparisons_only", notes
    return "authored_conclusion_recorded", notes


def proposal_review_coverage(
    *, proposal_id: str, known_at: datetime, db_path: Path | None = None
) -> dict[str, object]:
    cutoff = _datetime(known_at)
    with closing(connect_read_only(db_path)) as connection:
        connection.execute("BEGIN")
        header = connection.execute(
            "SELECT published_at FROM investment_proposal WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if header is None or _datetime(header[0]) > cutoff:
            raise RetrospectiveReadError("specified proposal was not available at the cutoff")
        proposal = load_investment_proposal(connection, proposal_id)
        erroneous = is_erroneous_proposal_as_of(connection, proposal_id, cutoff)
        rows = []
        for item in proposal.items:
            origin = ProposalItemRef(
                proposal_id=proposal_id,
                draft_sha256=proposal.review.draft_sha256,
                ticker=item.ticker,
            )
            status, notes = item_review_notes(connection, origin, proposal.published_at, cutoff)
            rows.append(
                {
                    **original_item_questions(item),
                    "coverage": status,
                    "retrospectives": notes,
                }
            )
        return {
            "proposal_id": proposal_id,
            "draft_sha256": proposal.review.draft_sha256,
            "published_at": proposal.published_at.isoformat(),
            "known_at": cutoff.isoformat(),
            "proposal_status": "corrected_as_erroneous" if erroneous else "published",
            "denominator": "all_items_of_this_exact_publication",
            "item_count": len(rows),
            "item_counts_by_coverage": dict(Counter(row["coverage"] for row in rows)),
            "items": rows,
        }
