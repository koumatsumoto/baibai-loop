"""Operationの有限対象と評価/提案への参照。本文の第二正本を作らない。"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalResearchScope,
    ReviewedOutcome,
    UnfinishedOutcome,
)
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.selection import (
    ResearchSelectionRecord as ResearchSelection,
)
from baibai_engine.investment_judgment.workflow.selection import (
    ResearchTarget,
    parse_selection,
    selection_sha256,
    selection_targets,
)


class ReviewedTarget(ReviewedOutcome):
    """新規/保有に共通の公開済み評価pairへの正確な参照。"""

    status: Literal["reviewed"] = "reviewed"


class UnfinishedTarget(UnfinishedOutcome):
    status: Literal["unfinished"] = "unfinished"


type ResearchOutcome = Annotated[ReviewedTarget | UnfinishedTarget, Field(discriminator="status")]


class ProposalRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str = Field(
        pattern=r"^investment-proposal-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResearchProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["research_progress"] = "research_progress"
    outcomes: tuple[ResearchOutcome, ...] = ()
    proposal_ref: ProposalRef | None = None

    @model_validator(mode="after")
    def unique_subjects(self) -> Self:
        if len({item.ticker for item in self.outcomes}) != len(self.outcomes):
            raise ValueError("a target must have exactly one reviewed or unfinished outcome")
        return self


def selection_binding(payload: OperationPayload) -> ResearchSelection | None:
    rows = [item for item in payload.artifacts if item.get("kind") == "research_selection"]
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("one fixed research_selection is required")
    return parse_selection(rows[0])


def progress_binding(payload: OperationPayload) -> ResearchProgress:
    rows = [item for item in payload.artifacts if item.get("kind") == "research_progress"]
    if len(rows) > 1:
        raise ValueError("one research_progress is required")
    return ResearchProgress() if not rows else ResearchProgress.model_validate(rows[0])


def require_coverage(
    targets: tuple[ResearchTarget, ...], outcomes: tuple[ResearchOutcome, ...], *, complete: bool
) -> None:
    selected = {(item.ticker, item.purpose) for item in targets}
    actual = {(item.ticker, item.purpose) for item in outcomes}
    if len(actual) != len(outcomes) or not actual <= selected:
        raise ValueError("research outcomes contain duplicate or unselected ticker/purpose")
    if complete and actual != selected:
        raise ValueError("selected targets must be covered by reviewed and unfinished outcomes")


def proposal_scope(operation_id: str, payload: OperationPayload) -> ProposalResearchScope:
    selection = selection_binding(payload)
    if selection is None:
        raise ValueError("fixed research_selection is required")
    progress = progress_binding(payload)
    require_coverage(selection_targets(selection), progress.outcomes, complete=True)
    return ProposalResearchScope.model_validate(
        {
            "operation_id": operation_id,
            "selection_sha256": selection_sha256(selection),
            "targets": [item.model_dump(mode="json") for item in selection_targets(selection)],
            "outcomes": [item.model_dump(mode="json") for item in progress.outcomes],
        }
    )


def with_progress(payload: OperationPayload, progress: ResearchProgress) -> OperationPayload:
    return payload.model_copy(
        update={
            "artifacts": (
                *(item for item in payload.artifacts if item.get("kind") != "research_progress"),
                progress.model_dump(mode="json"),
            )
        }
    )
