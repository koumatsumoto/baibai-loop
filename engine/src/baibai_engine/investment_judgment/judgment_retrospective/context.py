"""保存と再訪で同じ根拠・台帳断面を照合する。"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from baibai_engine.investment_judgment.company_research.document import ResearchRef
from baibai_engine.investment_judgment.company_research.store import require_research_ref
from baibai_engine.investment_judgment.judgment_retrospective.document import (
    LedgerSnapshot,
    ProposalItemRef,
    RetrospectiveDocument,
    RetrospectiveOrigin,
    RetrospectiveRecord,
)
from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment.store import (
    ReviewedAssessment,
    require_assessment_ref,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import proposal_draft_sha256
from baibai_engine.investment_judgment.trade_proposal.query import load_investment_proposal
from baibai_engine.portfolio.ledger.read import load_ledger_events, related_decision_events
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.time import JST


def assessment_reference(pair: ReviewedAssessment) -> AssessmentRef:
    return AssessmentRef(
        assessment_id=pair.assessment_id,
        core_sha256=pair.core_sha256,
        review_id=pair.review.review_id,
    )


def _assessment_context(pair: ReviewedAssessment) -> dict[str, object]:
    return {
        "reference": assessment_reference(pair).model_dump(mode="json"),
        "published_at": pair.published_at.isoformat(),
        "document": pair.document.model_dump(mode="json"),
        "review": pair.review.model_dump(mode="json"),
    }


def origin_context(
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
        proposal_context = proposal.model_dump(mode="json", exclude={"items"})
        proposal_context["item"] = item.model_dump(mode="json")
        if item.assessment_ref is not None:
            pair = require_assessment_ref(connection, item.assessment_ref)
    else:
        pair = require_assessment_ref(connection, origin)
        published_at = pair.published_at
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
        "assessment": None if pair is None else _assessment_context(pair),
    }


def capture_ledger_snapshot(
    connection: sqlite3.Connection,
    *,
    origin: RetrospectiveOrigin,
    ticker: str,
    as_of: date,
    append_head: int | None,
) -> LedgerSnapshot | None:
    if append_head is None:
        return None
    if not isinstance(origin, ProposalItemRef):
        raise ValueError("Assessment-only retrospective has no order reference")
    events = related_decision_events(
        connection,
        reference=origin.proposal_id,
        ticker=ticker,
        append_head=append_head,
        as_of=as_of,
    )
    return LedgerSnapshot(append_head=append_head, event_ids=tuple(e.event_id for e in events))


def ledger_context(
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
                    saved_at = pair.published_at
                    sources, facts = snapshot.sources, snapshot.facts
                    content = _assessment_context(pair)
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
        "origin": origin_context(
            connection,
            document.origin,
            ticker=document.ticker,
            as_of=document.as_of,
            recorded_at=record.recorded_at,
        ),
        "ledger": ledger_context(connection, record.ledger, as_of=document.as_of),
        "citations": _citation_contexts(connection, document, recorded_at=record.recorded_at),
    }
