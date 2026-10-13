"""Operationが所有する有限対象と成果。判断・取引事実の第二正本を作らない。"""

from __future__ import annotations

from datetime import date
from hashlib import sha256
from typing import Annotated, Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from baibai_engine.research.operation.models import OperationPayload
from baibai_engine.research.position_review import PositionReviewDocument
from baibai_engine.storage.application.json import canonical_json


class ExplicitResearchRun(BaseModel):
    """開始時にcanonical runから引用した、選択銘柄だけの機械根拠。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 候補順位は開始資格ではない。明示要求はTriageの優先選別と別の開始根拠。
    # run整理後も当時の根拠を読めるよう、選択銘柄だけを開始時に固定する。
    # docs/reference/analysis-operations.md#explicit-research-source / refs #1481

    kind: Literal["user_selected_run"] = "user_selected_run"
    run_revision_id: str = Field(min_length=1)
    as_of: date
    run_at: AwareDatetime
    run_metadata: dict[str, Any]
    analyses: dict[str, dict[str, Any]]

    @model_validator(mode="after")
    def ticker_identity(self) -> Self:
        if any(row.get("ticker") != ticker for ticker, row in self.analyses.items()):
            raise ValueError("explicit run analysis ticker differs from its key")
        return self

    def binding(self) -> dict[str, str]:
        return {
            "run_revision_id": self.run_revision_id,
            "snapshot_sha256": sha256(
                canonical_json(self.model_dump(mode="json")).encode()
            ).hexdigest(),
        }


class ResearchTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    purpose: Literal["new", "holding"]


class ResearchSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["research_selection"] = "research_selection"
    request: str = Field(min_length=1, pattern=r"\S")
    research_triage_id: str | None = None
    explicit_run: ExplicitResearchRun | None = Field(default=None, exclude_if=lambda v: v is None)
    targets: tuple[ResearchTarget, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def fixed_subjects(self) -> Self:
        if len({item.ticker for item in self.targets}) != len(self.targets):
            raise ValueError("selected tickers must be unique across new and holding purposes")
        if self.research_triage_id and self.explicit_run is not None:
            raise ValueError("Research Triage and explicit run are mutually exclusive sources")
        if self.explicit_run is not None and set(self.explicit_run.analyses) != {
            item.ticker for item in self.targets
        }:
            raise ValueError("explicit analysis snapshot must exactly cover the fixed targets")
        if (
            any(item.purpose == "new" for item in self.targets)
            and not self.research_triage_id
            and self.explicit_run is None
        ):
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
