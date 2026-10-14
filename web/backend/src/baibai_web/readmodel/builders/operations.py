"""保存済みOperationを表示する。"""

from __future__ import annotations

from collections.abc import Mapping

from baibai_web.readmodel.builders.presentation import position_review_reason_text, text
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
        operations=[_operation_view(item, source) for item in source.operations()],
        outcomes=[PortfolioOutcomeView.model_validate(item) for item in source.outcomes()],
    )


def _operation_view(raw: Mapping[str, object], source: DbOperationsSource) -> OperationSessionView:
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
                    "selection_rationale": target.get("rationale"),
                    "research_question": target.get("research_question"),
                    "key_risk": target.get("key_risk"),
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
                    "holding_proposal_null_reason": position_review_reason_text(
                        source.position_review_null_reason(proposal)
                    )
                    if proposal is not None and proposal.get("action") is None
                    else None,
                }
            )
        )
    assessment = next(
        (item for item in artifacts if item.get("kind") == "capital_allocation_assessment"), {}
    )
    request = selection["request"]
    if not isinstance(request, Mapping) or selection.get("schema_version") != 2:
        raise ValueError("Operation requires an explicit selection V2 cutover")
    inputs = selection.get("inputs")
    analysis = inputs.get("analysis") if isinstance(inputs, Mapping) else {}
    analysis = analysis if isinstance(analysis, Mapping) else {}
    capacity = selection.get("capacity")
    capacity = capacity if isinstance(capacity, Mapping) else {}
    return view.model_copy(
        update={
            "research": OperationResearchView(
                request=str(request["text"]),
                request_scope=text(request.get("scope")),
                selection_summary=text(selection.get("selection_summary")),
                max_deep_research_targets=capacity.get("max_deep_research_targets"),
                targets=targets,
                capital_allocation_assessment_id=text(assessment.get("ref")),
                source_run_revision_id=text(analysis.get("run_revision_id")),
                source_analysis_as_of=analysis.get("analysis_as_of"),
            )
        }
    )
