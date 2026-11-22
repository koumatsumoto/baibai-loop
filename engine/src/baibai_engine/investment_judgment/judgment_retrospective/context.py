"""保存と再訪で同じ根拠・台帳断面を照合する。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

from baibai_engine.investment_judgment.company_research.document import ResearchRecord, ResearchRef
from baibai_engine.investment_judgment.company_research.store import require_research_ref
from baibai_engine.investment_judgment.judgment_retrospective.document import (
    LedgerSnapshot,
    ProposalItemRef,
    RetrospectiveCitation,
    RetrospectiveDocument,
    RetrospectiveOrigin,
    RetrospectiveRecord,
)
from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment.store import (
    ReviewedAssessment,
    require_assessment_ref,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalItem,
    TradeProposal,
)
from baibai_engine.investment_judgment.trade_proposal.query import (
    ProposalRecordReader,
    load_proposal_with_assessments,
    saved_proposal_market_context,
)
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReport,
    ProposalCorrection,
)
from baibai_engine.portfolio.ledger.events import LedgerEvent
from baibai_engine.portfolio.ledger.read import load_ledger_events, related_decision_events
from baibai_engine.primitives.time import JST

type _ProposalReceipts = tuple[list[ProposalCorrection], list[HumanProposalReport]]
type _CitedRecord = ResearchRecord | ReviewedAssessment
type _BoundCitation = tuple[RetrospectiveCitation, _CitedRecord]


@dataclass(frozen=True, slots=True)
class _BoundOrigin:
    published_at: datetime
    pair: ReviewedAssessment | None
    proposal: TradeProposal | None = None
    item: ProposalItem | None = None
    market_context: dict[str, object] | None = None
    receipts: _ProposalReceipts | None = None


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


def _read_proposal_receipts(
    connection: sqlite3.Connection, proposal: TradeProposal
) -> _ProposalReceipts | None:
    try:
        return ProposalRecordReader(connection).read(proposal)
    except (ValueError, TypeError, FileNotFoundError):
        # Optional later receipts must not invalidate the exact original judgment.
        return None


def _proposal_record_context(
    records: _ProposalReceipts | None, *, recorded_at: datetime
) -> dict[str, object]:
    result: dict[str, object] = {
        "source": "proposal_receipts",
        "scope": "whole_proposal",
        "recorded_through": recorded_at.astimezone(UTC).isoformat(),
        "status": "unavailable",
        "records": None,
    }
    if records is None:
        return result
    corrections, reports = records
    return {
        **result,
        "status": "retained",
        "records": {
            "corrections": [
                record.model_dump(mode="json")
                for record in corrections
                if record.recorded_at <= recorded_at
            ],
            "human_reports": [
                report.model_dump(mode="json")
                for report in reports
                if report.recorded_at <= recorded_at
            ],
        },
    }


def _read_origin(
    connection: sqlite3.Connection,
    origin: RetrospectiveOrigin,
    *,
    ticker: str,
    as_of: date,
    recorded_at: datetime,
) -> _BoundOrigin:
    if isinstance(origin, ProposalItemRef):
        proposal, pairs = load_proposal_with_assessments(connection, origin.proposal_id)
        if proposal.review.draft_sha256 != origin.draft_sha256:
            raise ValueError("Proposal reference differs from stored content")
        item = next((item for item in proposal.items if item.ticker == origin.ticker), None)
        if item is None or origin.ticker != ticker:
            raise ValueError("origin Proposal item ticker mismatch")
        bound = _BoundOrigin(
            published_at=proposal.published_at,
            pair=None if item.assessment_ref is None else pairs[item.assessment_ref.assessment_id],
            proposal=proposal,
            item=item,
            market_context=saved_proposal_market_context(connection, proposal),
            receipts=_read_proposal_receipts(connection, proposal),
        )
    else:
        pair = require_assessment_ref(connection, origin)
        bound = _BoundOrigin(published_at=pair.published_at, pair=pair)
    if bound.pair is not None and bound.pair.document.input_snapshot.ticker != ticker:
        raise ValueError("origin Assessment ticker mismatch")
    if bound.published_at > recorded_at or bound.published_at.astimezone(JST).date() > as_of:
        raise ValueError("origin publication is after retrospective information/recording time")
    if as_of > recorded_at.astimezone(JST).date():
        raise ValueError("Retrospective as_of is after recorded_at")
    return bound


def _origin_view(
    origin: RetrospectiveOrigin, bound: _BoundOrigin, *, recorded_at: datetime
) -> dict[str, object]:
    proposal_context = record_context = None
    if bound.proposal is not None:
        assert bound.item is not None
        proposal_context = bound.proposal.model_dump(mode="json", exclude={"items"})
        proposal_context["item"] = bound.item.model_dump(mode="json")
        record_context = _proposal_record_context(bound.receipts, recorded_at=recorded_at)
    return {
        "reference": origin.model_dump(mode="json"),
        "published_at": bound.published_at.isoformat(),
        "proposal": proposal_context,
        "assessment": None if bound.pair is None else _assessment_context(bound.pair),
        "market_context": bound.market_context,
        "proposal_records": record_context,
    }


def origin_context(
    connection: sqlite3.Connection,
    origin: RetrospectiveOrigin,
    *,
    ticker: str,
    as_of: date,
    recorded_at: datetime,
) -> dict[str, object]:
    bound = _read_origin(connection, origin, ticker=ticker, as_of=as_of, recorded_at=recorded_at)
    return _origin_view(origin, bound, recorded_at=recorded_at)


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


def _read_ledger_snapshot(
    connection: sqlite3.Connection, ledger: LedgerSnapshot | None, *, as_of: date
) -> tuple[LedgerEvent, ...]:
    if ledger is None:
        return ()
    events = load_ledger_events(
        connection, append_head=ledger.append_head, event_ids=ledger.event_ids
    )
    if any(
        (event.occurred_on or event.occurred_at.astimezone(JST).date()) > as_of for event in events
    ):
        raise ValueError("ledger snapshot event is after retrospective as_of")
    return events


def _ledger_view(
    ledger: LedgerSnapshot | None, events: tuple[LedgerEvent, ...]
) -> dict[str, object] | None:
    if ledger is None:
        return None
    return {
        "append_head": ledger.append_head,
        "events": [event.model_dump(mode="json") for event in events],
    }


def ledger_context(
    connection: sqlite3.Connection, ledger: LedgerSnapshot | None, *, as_of: date
) -> dict[str, object] | None:
    return _ledger_view(ledger, _read_ledger_snapshot(connection, ledger, as_of=as_of))


def _read_citations(
    connection: sqlite3.Connection, document: RetrospectiveDocument, *, recorded_at: datetime
) -> list[_BoundCitation]:
    citations: list[_BoundCitation] = []
    # Local to this read transaction; source IDs never share scope across documents.
    loaded: dict[ResearchRef | AssessmentRef, tuple[set[str], set[str], _CitedRecord]] = {}
    for comparison in document.comparisons:
        for citation in comparison.citations:
            reference = citation.reference
            if reference not in loaded:
                record: _CitedRecord
                if isinstance(reference, ResearchRef):
                    research = require_research_ref(connection, reference)
                    ticker, as_of = research.document.ticker, research.document.as_of
                    saved_at = research.recorded_at
                    sources, facts = research.document.sources, research.document.facts
                    record = research
                else:
                    pair = require_assessment_ref(connection, reference)
                    snapshot = pair.document.input_snapshot
                    ticker, as_of = snapshot.ticker, snapshot.as_of
                    saved_at = pair.published_at
                    sources, facts = snapshot.sources, snapshot.facts
                    record = pair
                if ticker != document.ticker:
                    raise ValueError("citation ticker mismatch")
                if as_of > document.as_of or saved_at > recorded_at:
                    raise ValueError("citation is after retrospective information/recording time")
                loaded[reference] = (
                    {source.source_id for source in sources},
                    {fact.fact_id for fact in facts},
                    record,
                )
            source_ids, fact_ids, record = loaded[reference]
            if not set(citation.source_ids) <= source_ids:
                raise ValueError("citation has unknown source ID in its referenced document")
            if not set(citation.fact_ids) <= fact_ids:
                raise ValueError("citation has unknown fact ID in its referenced document")
            citations.append((citation, record))
    return citations


def _citation_views(citations: list[_BoundCitation]) -> list[dict[str, object]]:
    contexts: list[dict[str, object]] = []
    rendered: dict[ResearchRef | AssessmentRef, dict[str, object]] = {}
    for citation, record in citations:
        reference = citation.reference
        if reference not in rendered:
            rendered[reference] = (
                record.model_dump(mode="json")
                if isinstance(record, ResearchRecord)
                else _assessment_context(record)
            )
        contexts.append(
            {"citation": citation.model_dump(mode="json"), "record": rendered[reference]}
        )
    return contexts


def _read_retrospective_context(
    connection: sqlite3.Connection, record: RetrospectiveRecord
) -> tuple[_BoundOrigin, tuple[LedgerEvent, ...], list[_BoundCitation]]:
    document = record.document
    return (
        _read_origin(
            connection,
            document.origin,
            ticker=document.ticker,
            as_of=document.as_of,
            recorded_at=record.recorded_at,
        ),
        _read_ledger_snapshot(connection, record.ledger, as_of=document.as_of),
        _read_citations(connection, document, recorded_at=record.recorded_at),
    )


def validate_retrospective_context(
    connection: sqlite3.Connection, record: RetrospectiveRecord
) -> None:
    """Validate the same saved references as a revisit without producing display JSON."""
    _read_retrospective_context(connection, record)


def retrospective_context(
    connection: sqlite3.Connection, record: RetrospectiveRecord
) -> dict[str, object]:
    origin, events, citations = _read_retrospective_context(connection, record)
    return {
        "origin": _origin_view(record.document.origin, origin, recorded_at=record.recorded_at),
        "ledger": _ledger_view(record.ledger, events),
        "citations": _citation_views(citations),
    }
