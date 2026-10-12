"""Operationが所有する有限対象と成果。判断・取引事実の第二正本を作らない。"""

from __future__ import annotations

from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from baibai_engine.appdb.json import canonical_json
from baibai_engine.research.operation.models import OperationPayload
from baibai_engine.research.position_review import PositionReviewDocument


class ResearchTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    purpose: Literal["new", "holding"]


class ResearchSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["research_selection"] = "research_selection"
    request: str = Field(min_length=1, pattern=r"\S")
    research_triage_id: str | None = None
    targets: tuple[ResearchTarget, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def fixed_subjects(self) -> Self:
        if len({item.ticker for item in self.targets}) != len(self.targets):
            raise ValueError("selected tickers must be unique across new and holding purposes")
        if any(item.purpose == "new" for item in self.targets) and not self.research_triage_id:
            raise ValueError("new research targets require Research Triage admission")
        return self


class ReviewedTarget(ResearchTarget):
    purpose: Literal["new"] = "new"
    status: Literal["reviewed"] = "reviewed"
    thesis_id: str = Field(min_length=1)
    thesis_core_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    thesis_review_id: str = Field(min_length=1)


class HoldingProposal(ResearchTarget):
    purpose: Literal["holding"] = "holding"
    status: Literal["proposal"] = "proposal"
    draft: PositionReviewDocument
    checked_at: AwareDatetime | None = None
    thesis_core_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    thesis_review_id: str = Field(min_length=1)


class UnfinishedTarget(ResearchTarget):
    status: Literal["unfinished"] = "unfinished"
    reason: str = Field(min_length=1, pattern=r"\S")
    references: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def references_present(self) -> Self:
        if any(not value.strip() for value in self.references):
            raise ValueError("unfinished references must be non-empty")
        return self


type ResearchOutcome = Annotated[
    ReviewedTarget | HoldingProposal | UnfinishedTarget, Field(discriminator="status")
]


class ResearchProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["research_progress"] = "research_progress"
    outcomes: tuple[ResearchOutcome, ...] = ()

    @model_validator(mode="after")
    def unique_subjects(self) -> Self:
        if len({item.ticker for item in self.outcomes}) != len(self.outcomes):
            raise ValueError("a target must have exactly one completed or unfinished outcome")
        return self


class ResearchComparisonScope(BaseModel):
    """CAAに固定する新規完了・未完了の範囲。保有提案は比較へ混ぜない。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    targets: tuple[ResearchTarget, ...]
    outcomes: tuple[ResearchOutcome, ...]

    @model_validator(mode="after")
    def exact_cover(self) -> Self:
        if any(item.purpose != "new" for item in (*self.targets, *self.outcomes)):
            raise ValueError("CAA scope contains only new research targets")
        require_coverage(self.targets, self.outcomes, complete=True)
        return self


def selection_binding(payload: OperationPayload) -> ResearchSelection | None:
    rows = [item for item in payload.artifacts if item.get("kind") == "research_selection"]
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("one fixed research_selection is required")
    return ResearchSelection.model_validate(rows[0])


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
        raise ValueError("selected targets must be covered by completed and unfinished outcomes")


def comparison_scope(operation_id: str, payload: OperationPayload) -> ResearchComparisonScope:
    selection = selection_binding(payload)
    if selection is None:
        raise ValueError("fixed research_selection is required")
    progress = progress_binding(payload)
    return ResearchComparisonScope(
        operation_id=operation_id,
        targets=tuple(item for item in selection.targets if item.purpose == "new"),
        outcomes=tuple(item for item in progress.outcomes if item.purpose == "new"),
    )


def selection_sha256(selection: ResearchSelection) -> str:
    return sha256(canonical_json(selection.model_dump(mode="json")).encode()).hexdigest()


def with_progress(payload: OperationPayload, progress: ResearchProgress) -> OperationPayload:
    return payload.model_copy(
        update={
            "artifacts": (
                *(item for item in payload.artifacts if item.get("kind") != "research_progress"),
                progress.model_dump(mode="json"),
            )
        }
    )
