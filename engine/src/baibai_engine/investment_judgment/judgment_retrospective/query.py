"""当時判断・後日証拠・記録済み取引を別々にexact読取する。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.investment_judgment.company_research.document import ResearchRef
from baibai_engine.investment_judgment.company_research.store import require_research_ref
from baibai_engine.investment_judgment.contracts import _datetime
from baibai_engine.investment_judgment.judgment_retrospective.document import (
    LedgerSnapshot,
    ProposalItemRef,
    RetrospectiveDocument,
    RetrospectiveOrigin,
    RetrospectiveRecord,
)
from baibai_engine.investment_judgment.judgment_retrospective.store import (
    RetrospectiveReadError,
    load_retrospective,
    validate_correction,
)
from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment.store import (
    ReviewedAssessment,
    load_reviewed_assessment,
    require_assessment_ref,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import proposal_draft_sha256
from baibai_engine.investment_judgment.trade_proposal.query import load_investment_proposal
from baibai_engine.portfolio.ledger.read import (
    load_ledger_events,
    optional_ledger_head,
    related_decision_events,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only


def _assessment_reference(pair: ReviewedAssessment) -> AssessmentRef:
    return AssessmentRef(
        assessment_id=pair.assessment_id,
        core_sha256=pair.core_sha256,
        review_id=pair.review.review_id,
    )


def _assessment_publication(connection: sqlite3.Connection, assessment_id: str) -> datetime:
    row = connection.execute(
        "SELECT published_at FROM stock_assessment WHERE assessment_id=?", (assessment_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown assessment: {assessment_id}")
    return _datetime(str(row[0]))


def _assessment_context(
    connection: sqlite3.Connection, pair: ReviewedAssessment
) -> dict[str, object]:
    return {
        "reference": _assessment_reference(pair).model_dump(mode="json"),
        "published_at": _assessment_publication(connection, pair.assessment_id).isoformat(),
        "document": pair.document.model_dump(mode="json"),
        "review": pair.review.model_dump(mode="json"),
    }


def _origin_context(
    connection: sqlite3.Connection,
    origin: RetrospectiveOrigin,
    *,
    ticker: str,
    as_of: date,
    recorded_at: datetime,
) -> dict[str, object]:
    proposal_context: dict[str, object] | None = None
    pair = None
    if isinstance(origin, ProposalItemRef):
        proposal = load_investment_proposal(connection, origin.proposal_id)
        if proposal_draft_sha256(proposal) != origin.draft_sha256:
            raise ValueError("Proposal reference differs from stored content")
        item = next((item for item in proposal.items if item.ticker == origin.ticker), None)
        if item is None or origin.ticker != ticker:
            raise ValueError("origin Proposal item ticker mismatch")
        published_at = proposal.published_at
        proposal_context = {
            "proposal_id": proposal.proposal_id,
            "as_of": proposal.as_of.isoformat(),
            "published_at": published_at.isoformat(),
            "headline": proposal.headline,
            "comparison": proposal.comparison,
            "forgone": proposal.forgone,
            "item": item.model_dump(mode="json"),
            "inputs": proposal.inputs.model_dump(mode="json"),
            "review": proposal.review.model_dump(mode="json"),
        }
        if item.assessment_ref is not None:
            pair = require_assessment_ref(connection, item.assessment_ref)
    else:
        pair = require_assessment_ref(connection, origin)
        published_at = _assessment_publication(connection, origin.assessment_id)
    if pair is not None and pair.document.input_snapshot.ticker != ticker:
        raise ValueError("origin Assessment ticker mismatch")
    if published_at > recorded_at or published_at.astimezone(JST).date() > as_of:
        raise ValueError("origin publication is after retrospective information/recording time")
    if as_of > recorded_at.astimezone(JST).date():
        raise ValueError("Retrospective as_of is after recorded_at")
    return {
        "reference": origin.model_dump(mode="json"),
        "published_at": published_at.isoformat(),
        "proposal": proposal_context,
        "assessment": None if pair is None else _assessment_context(connection, pair),
    }


def capture_ledger_snapshot(
    connection: sqlite3.Connection,
    document: RetrospectiveDocument,
    *,
    append_head: int | None,
) -> LedgerSnapshot | None:
    if append_head is None:
        return None
    if not isinstance(document.origin, ProposalItemRef):
        raise ValueError("Assessment-only retrospective has no order reference")
    events = related_decision_events(
        connection,
        reference=document.origin.proposal_id,
        ticker=document.ticker,
        append_head=append_head,
        as_of=document.as_of,
    )
    return LedgerSnapshot(append_head=append_head, event_ids=tuple(e.event_id for e in events))


def _ledger_context(
    connection: sqlite3.Connection, ledger: LedgerSnapshot | None, *, as_of: date
) -> dict[str, object] | None:
    if ledger is None:
        return None
    events = load_ledger_events(
        connection, append_head=ledger.append_head, event_ids=ledger.event_ids
    )
    if any(
        (event.occurred_on or event.occurred_at.astimezone(JST).date()) > as_of for event in events
    ):
        raise ValueError("ledger snapshot event is after retrospective as_of")
    return {
        "append_head": ledger.append_head,
        "events": [event.model_dump(mode="json") for event in events],
    }


def _citation_contexts(
    connection: sqlite3.Connection, document: RetrospectiveDocument, *, recorded_at: datetime
) -> list[dict[str, object]]:
    contexts: list[dict[str, object]] = []
    # Local to this read transaction; source IDs never share scope across documents.
    loaded: dict[str, tuple[set[str], set[str], dict[str, object]]] = {}
    for comparison in document.comparisons:
        for citation in comparison.citations:
            key = canonical_json(citation.reference.model_dump(mode="json"))
            if key not in loaded:
                if isinstance(citation.reference, ResearchRef):
                    research = require_research_ref(connection, citation.reference)
                    ticker, as_of = research.document.ticker, research.document.as_of
                    saved_at = research.recorded_at
                    sources, facts = research.document.sources, research.document.facts
                    content = research.model_dump(mode="json")
                else:
                    pair = require_assessment_ref(connection, citation.reference)
                    snapshot = pair.document.input_snapshot
                    ticker, as_of = snapshot.ticker, snapshot.as_of
                    saved_at = _assessment_publication(connection, pair.assessment_id)
                    sources, facts = snapshot.sources, snapshot.facts
                    content = _assessment_context(connection, pair)
                if ticker != document.ticker:
                    raise ValueError("citation ticker mismatch")
                if as_of > document.as_of or saved_at > recorded_at:
                    raise ValueError("citation is after retrospective information/recording time")
                loaded[key] = (
                    {source.source_id for source in sources},
                    {fact.fact_id for fact in facts},
                    content,
                )
            source_ids, fact_ids, content = loaded[key]
            if not set(citation.source_ids) <= source_ids:
                raise ValueError("citation has unknown source ID in its referenced document")
            if not set(citation.fact_ids) <= fact_ids:
                raise ValueError("citation has unknown fact ID in its referenced document")
            contexts.append({"citation": citation.model_dump(mode="json"), "record": content})
    return contexts


def retrospective_context(
    connection: sqlite3.Connection, record: RetrospectiveRecord
) -> dict[str, object]:
    document = record.document
    return {
        "origin": _origin_context(
            connection,
            document.origin,
            ticker=document.ticker,
            as_of=document.as_of,
            recorded_at=record.recorded_at,
        ),
        "ledger": _ledger_context(connection, record.ledger, as_of=document.as_of),
        "citations": _citation_contexts(connection, document, recorded_at=record.recorded_at),
    }


def scaffold_retrospective(
    *,
    retrospective_id: str,
    as_of: date,
    ticker: str | None = None,
    proposal_id: str | None = None,
    assessment_id: str | None = None,
    milestone: str = "",
    db_path: Path | None = None,
    known_at: datetime | None = None,
) -> dict[str, object]:
    if (proposal_id is None) == (assessment_id is None):
        raise ValueError("choose exactly one origin: proposal_id or assessment_id")
    now = _datetime(known_at or datetime.now(JST))
    with closing(connect_read_only(db_path)) as connection:
        connection.execute("BEGIN")
        origin: RetrospectiveOrigin
        head = None
        ledger = None
        if proposal_id is not None:
            if ticker is None:
                raise ValueError("Proposal origin requires ticker")
            ticker = normalize_ticker(ticker)
            proposal = load_investment_proposal(connection, proposal_id)
            origin = ProposalItemRef(
                proposal_id=proposal_id,
                draft_sha256=proposal_draft_sha256(proposal),
                ticker=ticker,
            )
            head = optional_ledger_head(connection)
            if head is not None:
                events = related_decision_events(
                    connection,
                    reference=proposal_id,
                    ticker=ticker,
                    append_head=head,
                    as_of=as_of,
                )
                ledger = LedgerSnapshot(append_head=head, event_ids=tuple(e.event_id for e in events))
        else:
            assert assessment_id is not None
            pair = load_reviewed_assessment(connection, assessment_id)
            origin = _assessment_reference(pair)
            actual_ticker = pair.document.input_snapshot.ticker
            if ticker is not None and normalize_ticker(ticker) != actual_ticker:
                raise ValueError("origin Assessment ticker mismatch")
            ticker = actual_ticker
        context = {
            "origin": _origin_context(
                connection, origin, ticker=ticker, as_of=as_of, recorded_at=now
            ),
            "ledger": _ledger_context(connection, ledger, as_of=as_of),
            "citations": [],
        }
        # This is deliberately an unfinished JSON draft, not a valid empty document.
        return {
            "draft": {
                "retrospective_id": retrospective_id,
                "ledger_append_head": head,
                "document": {
                    "kind": "judgment_retrospective",
                    "schema_version": 1,
                    "ticker": ticker,
                    "as_of": as_of.isoformat(),
                    "milestone": milestone,
                    "origin": origin.model_dump(mode="json"),
                    "comparisons": [],
                    "lesson": None,
                    "corrects_ref": None,
                },
            },
            "context": context,
        }


def get_retrospective(
    *, retrospective_id: str, db_path: Path | None = None, sha256: str | None = None
) -> dict[str, object]:
    with closing(connect_read_only(db_path)) as connection:
        connection.execute("BEGIN")
        record = load_retrospective(connection, retrospective_id)
        if sha256 is not None and record.reference.payload_sha256 != sha256:
            raise RetrospectiveReadError("Retrospective reference differs from stored content")
        validate_correction(connection, record.document, recorded_at=record.recorded_at)
        return {
            "record": record.model_dump(mode="json"),
            "context": retrospective_context(connection, record),
        }


def list_retrospectives(
    *, db_path: Path | None = None, ticker: str | None = None, limit: int = 20
) -> dict[str, object]:
    if not 1 <= limit <= 100:
        raise ValueError("retrospective limit must be between 1 and 100")
    if ticker is not None:
        ticker = normalize_ticker(ticker)
    items: list[dict[str, object]] = []
    has_more = False
    resolved = database_path(db_path)
    if resolved.exists():
        with closing(connect_read_only(resolved)) as connection:
            connection.execute("BEGIN")
            if connection.execute("PRAGMA user_version").fetchone()[0] != 0:
                where, parameters = ("", ()) if ticker is None else ("WHERE ticker=? ", (ticker,))
                rows = connection.execute(
                    "SELECT retrospective_id FROM judgment_retrospective "
                    f"{where}ORDER BY recorded_at DESC,retrospective_id DESC LIMIT ?",
                    (*parameters, limit + 1),
                ).fetchall()
                has_more = len(rows) > limit
                for row in rows[:limit]:
                    record = load_retrospective(connection, str(row[0]))
                    document = record.document
                    items.append(
                        {
                            "reference": record.reference.model_dump(mode="json"),
                            "ticker": document.ticker,
                            "as_of": document.as_of.isoformat(),
                            "recorded_at": record.recorded_at.isoformat(),
                            "origin": document.origin.model_dump(mode="json"),
                            "milestone": document.milestone,
                            "lesson": None
                            if document.lesson is None
                            else document.lesson.next_time[:240],
                            "corrects_ref": None
                            if document.corrects_ref is None
                            else document.corrects_ref.model_dump(mode="json"),
                        }
                    )
    return {"items": items, "has_more": has_more, "limit": limit, "order": "recorded_at_desc"}
