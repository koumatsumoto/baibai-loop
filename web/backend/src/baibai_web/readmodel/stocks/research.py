"""保存評価pairと提案条件を投影し、現在previewを別viewで返す。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path

from baibai_web.readmodel.models import (
    InvestmentProposalItemView,
    InvestmentProposalPreviewView,
    InvestmentProposalSummaryView,
    InvestmentProposalsView,
    InvestmentProposalView,
    ResearchRevisionView,
    StockAssessmentDetailView,
    StockAssessmentsView,
    UnfinishedResearchView,
)
from baibai_web.readmodel.stocks.presentation import _mapping_items, _mapping_optional
from baibai_web.sources.protocols import ResearchSource
from baibai_web.sources.types import ResearchRevision, StockAssessmentDetail


class PublicationCursorError(ValueError):
    pass


def _page[T](
    rows: list[T], *, ids: list[str], limit: int | None, cursor: str | None
) -> tuple[list[T], str | None]:
    if limit is not None and not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    offset = 0
    if cursor is not None:
        if cursor not in ids:
            raise PublicationCursorError("cursor does not identify a publication in this result")
        offset = ids.index(cursor) + 1
    size = len(rows) if limit is None else limit
    result = rows[offset : offset + size]
    next_cursor = ids[offset + size - 1] if result and offset + size < len(rows) else None
    return result, next_cursor


def build_stock_assessment_list(
    research: ResearchSource,
    *,
    ticker: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> StockAssessmentsView:
    revisions = [item for item in research.revisions() if ticker is None or item.ticker == ticker]
    rows, next_cursor = _page(
        revisions, ids=[item.assessment_id for item in revisions], limit=limit, cursor=cursor
    )
    return StockAssessmentsView(
        assessments=[_research_revision_view(item) for item in rows], next_cursor=next_cursor
    )


def build_stock_assessment_detail(
    research: ResearchSource, *, assessment_id: str
) -> StockAssessmentDetailView | None:
    detail = research.assessment_detail(assessment_id)
    return None if detail is None else _assessment_detail_view(detail)


def build_proposal_list(
    research: ResearchSource,
    *,
    scope: str | None = None,
    ticker: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> InvestmentProposalsView:
    rows = [
        row
        for row in research.proposals()
        if (scope is None or row["request_scope"] == scope)
        and (
            ticker is None or any(item["ticker"] == ticker for item in _mapping_items(row["items"]))
        )
    ]
    selected, next_cursor = _page(
        rows, ids=[str(item["proposal_id"]) for item in rows], limit=limit, cursor=cursor
    )
    return InvestmentProposalsView(
        proposals=[_proposal_summary_view(item) for item in selected], next_cursor=next_cursor
    )


def build_proposal_detail(
    research: ResearchSource, *, proposal_id: str
) -> InvestmentProposalView | None:
    raw = research.proposal(proposal_id)
    if raw is None:
        return None
    scope = _mapping_optional(raw["research_scope"])
    review = _mapping_optional(raw["review"])
    return InvestmentProposalView.model_validate(
        {
            "proposal_id": raw["proposal_id"],
            "as_of": raw["as_of"],
            "prepared_at": raw["prepared_at"],
            "published_at": raw["published_at"],
            "request_scope": raw["request_scope"],
            "headline": raw["headline"],
            "comparison": raw["comparison"],
            "forgone": raw["forgone"],
            "market_context_assessment_id": raw.get("market_context_assessment_id"),
            "research_scope": scope,
            "saved_inputs": raw["inputs"],
            "items": [_proposal_item_view(item) for item in _mapping_items(raw["items"])],
            "review": review,
            "draft_sha256": raw.get("draft_sha256", review["draft_sha256"]),
            "liquidity_context": raw["liquidity_context"],
            "warnings": raw["warnings"],
            "portfolio_exposure": raw["portfolio_exposure"],
            "unfinished_research": [
                UnfinishedResearchView.model_validate(item)
                for item in _mapping_items(scope["outcomes"])
                if item["status"] == "unfinished"
            ],
        }
    )


def build_proposal_preview(
    research: ResearchSource,
    *,
    proposal_id: str,
    sqlite_path: Path,
    now: datetime,
) -> InvestmentProposalPreviewView | None:
    preview = research.proposal_preview(proposal_id, sqlite_path=sqlite_path, now=now)
    return None if preview is None else InvestmentProposalPreviewView.model_validate(preview)


def _proposal_summary_view(raw: Mapping[str, object]) -> InvestmentProposalSummaryView:
    items = _mapping_items(raw["items"])
    return InvestmentProposalSummaryView(
        proposal_id=str(raw["proposal_id"]),
        as_of=date.fromisoformat(str(raw["as_of"])),
        published_at=datetime.fromisoformat(str(raw["published_at"])),
        request_scope=str(raw["request_scope"]),
        headline=str(raw["headline"]),
        item_count=len(items),
        tickers=[str(item["ticker"]) for item in items],
        actions=[str(item["action"]) for item in items],
        research_operation_id=str(_mapping_optional(raw["research_scope"])["operation_id"]),
    )


def _proposal_item_view(raw: Mapping[str, object]) -> InvestmentProposalItemView:
    action = raw["action"]
    readiness = "not_applicable"
    if action in {"buy", "sell"}:
        readiness = "ready" if raw.get("terms") is not None else "needs_terms"
    return InvestmentProposalItemView.model_validate({**raw, "order_readiness": readiness})


def _research_revision_view(revision: ResearchRevision) -> ResearchRevisionView:
    return ResearchRevisionView(
        ticker=revision.ticker,
        company_name=revision.company_name,
        sector=revision.sector,
        summary=revision.summary,
        as_of=revision.as_of,
        published_at=revision.published_at,
        assessment_id=revision.assessment_id,
        stance=revision.stance,
        pmax_original_yen=revision.pmax_original_yen,
        review_id=revision.review_id,
        status=revision.status,
    )


def _assessment_detail_view(detail: StockAssessmentDetail) -> StockAssessmentDetailView:
    return StockAssessmentDetailView(
        revision=_research_revision_view(detail.revision),
        document=detail.document,
        review=detail.review,
        core_sha256=detail.core_sha256,
        projection=detail.projection,
    )
