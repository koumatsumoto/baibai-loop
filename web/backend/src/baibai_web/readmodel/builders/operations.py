"""保存済みOperationを表示する。"""

from __future__ import annotations

from collections.abc import Mapping

from baibai_web.readmodel.builders.presentation import text
from baibai_web.readmodel.models import (
    OperationResearchTargetView,
    OperationResearchView,
    OperationSessionView,
    OperationsView,
    PortfolioOutcomeView,
)
from baibai_web.readmodel.stocks.presentation import _mapping_items, _mapping_items_optional
from baibai_web.sources.db_sources import DbOperationsSource


def build_operations_view(source: DbOperationsSource) -> OperationsView:
    return OperationsView(
        operations=[_operation_view(item) for item in source.operations()],
        outcomes=[PortfolioOutcomeView.model_validate(item) for item in source.outcomes()],
    )


def _operation_view(raw: Mapping[str, object]) -> OperationSessionView:
    view = OperationSessionView.model_validate(raw)
    artifacts = _mapping_items_optional(view.payload.get("artifacts"))
    selection = next((item for item in artifacts if item.get("kind") == "research_selection"), None)
    if selection is None:
        return view
    progress = next((item for item in artifacts if item.get("kind") == "research_progress"), {})
    outcomes = {
        (item.get("ticker"), item.get("purpose")): item
        for item in _mapping_items_optional(progress.get("outcomes"))
    }
    targets = []
    for target in _mapping_items(selection.get("targets")):
        outcome = outcomes.get((target.get("ticker"), target.get("purpose")), {})
        proposal = outcome.get("draft")
        proposal = proposal if isinstance(proposal, dict) else None
        references = outcome.get("references")
        targets.append(
            OperationResearchTargetView.model_validate(
                {
                    "ticker": target["ticker"],
                    "purpose": target["purpose"],
                    "outcome_status": outcome.get("status"),
                    "thesis_id": text(outcome.get("thesis_id"))
                    or (text(proposal.get("thesis_id")) if proposal is not None else None),
                    "thesis_review_id": text(outcome.get("thesis_review_id")),
                    "unfinished_reason": text(outcome.get("reason")),
                    "references": [str(item) for item in references]
                    if isinstance(references, list)
                    else [],
                    "checked_at": outcome.get("checked_at"),
                    "holding_proposal": proposal,
                }
            )
        )
    assessment = next(
        (item for item in artifacts if item.get("kind") == "capital_allocation_assessment"), {}
    )
    return view.model_copy(
        update={
            "research": OperationResearchView(
                request=str(selection["request"]),
                research_triage_id=text(selection.get("research_triage_id")),
                targets=targets,
                capital_allocation_assessment_id=text(assessment.get("ref")),
            )
        }
    )
