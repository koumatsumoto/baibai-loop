"""企業評価・独立Review・CAAを表示する。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime

from baibai_web.readmodel.builders.presentation import number, text
from baibai_web.readmodel.models import (
    AllocationAlternativeView,
    AssessmentReviewView,
    CapitalAllocationAssessmentSummaryView,
    CapitalAllocationAssessmentView,
    ResearchRevisionView,
    ThesisDetailView,
)
from baibai_web.readmodel.stocks.presentation import _mapping_items_optional, _mapping_optional
from baibai_web.sources.protocols import (
    ResearchSource,
)
from baibai_web.sources.types import (
    ResearchRevision,
    ThesisDetail,
)


def build_assessment_detail(
    research: ResearchSource,
    *,
    capital_allocation_assessment_id: str,
) -> CapitalAllocationAssessmentView | None:
    """Build one published capital-allocation assessment for the detail page."""

    raw = research.assessment(capital_allocation_assessment_id)
    if raw is None:
        return None
    return _assessment_view(raw)


def _assessment_summary_view(raw: Mapping[str, object]) -> CapitalAllocationAssessmentSummaryView:
    alternatives = _mapping_items_optional(raw.get("alternatives"))
    allocated = next(
        (
            str(alternative["ticker"])
            for alternative in alternatives
            if alternative.get("disposition") == "allocate"
        ),
        None,
    )
    return CapitalAllocationAssessmentSummaryView(
        capital_allocation_assessment_id=str(raw["capital_allocation_assessment_id"]),
        as_of=date.fromisoformat(str(raw["as_of"])),
        published_at=datetime.fromisoformat(str(raw["published_at"])),
        decision=str(raw["result"]),
        headline=str(raw["headline"]),
        research_triage_id=str(raw["research_triage_id"]),
        alternative_count=len(alternatives),
        allocated_ticker=allocated,
    )


def _assessment_view(
    raw: Mapping[str, object],
) -> CapitalAllocationAssessmentView:
    return CapitalAllocationAssessmentView(
        capital_allocation_assessment_id=str(raw["capital_allocation_assessment_id"]),
        as_of=date.fromisoformat(str(raw["as_of"])),
        published_at=datetime.fromisoformat(str(raw["published_at"])),
        decision=str(raw["result"]),
        headline=str(raw["headline"]),
        research_triage_id=str(raw["research_triage_id"]),
        macro_context_id=text(raw.get("macro_context_id")),
        comparison=str(raw["comparison"]),
        foregone_alternatives=str(raw["forgone"]),
        alternatives=[
            _allocation_alternative_view(item)
            for item in _mapping_items_optional(raw.get("alternatives"))
        ],
        content_review=AssessmentReviewView.model_validate(raw["review"]),
    )


def _allocation_alternative_view(raw: Mapping[str, object]) -> AllocationAlternativeView:
    projection = raw.get("thesis_projection")
    machine_values = projection if isinstance(projection, Mapping) else {}
    return AllocationAlternativeView(
        ticker=str(raw["ticker"]),
        disposition=str(raw["disposition"]),
        rationale=str(raw["rationale"]),
        thesis_id=str(raw["thesis_id"]),
        thesis_review_id=text(raw.get("thesis_review_id")),
        case_status=text(_mapping_optional(machine_values.get("investment_case")).get("status")),
        base_annualized_return_pct=number(
            _mapping_optional(_mapping_optional(machine_values.get("projections")).get("base")).get(
                "annualized_return_pct"
            )
        ),
        pmax_raw_yen=number(machine_values.get("pmax_raw_yen")),
        valuation_as_of=text(machine_values.get("as_of")),
    )


def _research_revision_view(revision: ResearchRevision) -> ResearchRevisionView:
    return ResearchRevisionView(
        as_of=revision.as_of,
        thesis_id=revision.thesis_id,
        disposition=revision.disposition,
        pmax_raw_yen=revision.pmax_raw_yen,
        review_id=revision.review_id,
        status=revision.status,
    )


def _thesis_detail_view(detail: ThesisDetail) -> ThesisDetailView:
    return ThesisDetailView(
        revision=_research_revision_view(detail.revision), projection=detail.projection
    )
