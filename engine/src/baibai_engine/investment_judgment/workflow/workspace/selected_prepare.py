"""固定入力を一度保存し、再生成可能なworkspace投影を作る。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import EllipsisType
from typing import Literal, cast

from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.models import (
    OperationSession,
)
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import (
    OperationConflictError,
    OperationService,
)
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    GeneratedResearchSelectionV5,
    GeneratedSelection,
    ResearchScope,
    ResearchSelectionRecord,
    selection_sha256,
    validate_input_known_at,
)
from baibai_engine.investment_judgment.workflow.workspace.admission import (
    validate_workspace_selection,
    workspace_selection,
)
from baibai_engine.investment_judgment.workflow.workspace.files import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceError,
    _load_mapping,
    _write_workspace_file,
)


@dataclass(frozen=True, slots=True)
class SelectedResearchPreparation:
    status: Literal["prepared", "resumed", "already_completed"]
    operation_id: str
    workspace: Path
    scope: ResearchScope
    selected_tickers: tuple[str, ...]
    selected_count: int
    selection_sha256: str
    next_step: str | None


def _same_draft(
    selection: ResearchSelectionRecord,
    draft: GeneratedResearchSelectionDraft,
) -> bool:
    return (
        isinstance(selection, GeneratedSelection)
        and selection.request == draft.request
        and selection.research_as_of == draft.research_as_of
        and selection.input_known_at == draft.input_known_at
        and selection.targets == draft.targets
        and selection.selection_summary == draft.selection_summary
        and selection.inputs.generation.generation_id == draft.generation_id
        and selection.inputs.screening_selection_id == draft.screening_selection_id
        and (
            draft.expected_selection_sha256 is None
            or selection_sha256(selection) == draft.expected_selection_sha256
        )
        and (
            draft.screening_source_sha256 is None
            or (
                isinstance(selection, GeneratedResearchSelectionV5)
                and selection.inputs.screening_selection is not None
                and selection.inputs.screening_selection.source_sha256
                == draft.screening_source_sha256
            )
        )
        and (
            selection.comparison_tickers
            if isinstance(selection, GeneratedResearchSelectionV5)
            else ()
        )
        == draft.comparison_tickers
    )


def prepare_selected_workspace(
    *,
    draft: GeneratedResearchSelectionDraft,
    db_path: Path | None,
    workspace: Path,
    runs_path: Path,
    now: datetime,
    force: bool = False,
    company_growth_periods: int | EllipsisType | None = ...,
) -> SelectedResearchPreparation:
    if not isinstance(draft, GeneratedResearchSelectionDraft):
        raise ResearchWorkspaceConflictError("new Research requires a generated selection draft")
    try:
        validate_input_known_at(draft.input_known_at, now=now)
    except ValueError as error:
        raise ResearchWorkspaceConflictError(str(error)) from error
    service = OperationService(db_path, runs_path=runs_path)
    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists():
        manifest = _load_mapping(manifest_path, label="workspace manifest")
        manifest_selection = workspace_selection(manifest, db_path=db_path)
        if not _same_draft(manifest_selection, draft):
            raise ResearchWorkspaceConflictError("workspace has a different fixed selection")
        del manifest_selection
        return resume_selected_workspace(
            operation_id=str(manifest["operation_id"]),
            db_path=db_path,
            workspace=workspace,
            now=now,
            force=force,
            company_growth_periods=company_growth_periods,
        )
    active = service.active()
    if active is not None:
        saved = selection_binding(active.payload)
        if saved is None or not _same_draft(saved, draft):
            raise ResearchWorkspaceConflictError(
                f"different active Operation: {active.operation_id}"
            )
        return _materialize(
            active,
            saved,
            workspace,
            status="resumed",
            force=force,
            company_growth_periods=company_growth_periods,
        )
    try:
        committed, saved = service.start(draft, now=now)
    except OperationConflictError as error:
        raise ResearchWorkspaceConflictError(str(error)) from error
    return _materialize(
        committed,
        saved,
        workspace,
        status="prepared",
        force=force,
        company_growth_periods=company_growth_periods,
    )


def resume_selected_workspace(
    *,
    operation_id: str,
    db_path: Path | None,
    workspace: Path,
    now: datetime,
    force: bool = False,
    company_growth_periods: int | EllipsisType | None = ...,
) -> SelectedResearchPreparation:
    operation = OperationService(db_path).get(operation_id)
    selection: ResearchSelectionRecord | None
    if now.utcoffset() is None or now < operation.started_at:
        raise ResearchWorkspaceConflictError("resume precedes the Operation start")
    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists():
        manifest = _load_mapping(manifest_path, label="workspace manifest")
        if manifest.get("operation_id") != operation_id:
            raise ResearchWorkspaceConflictError("workspace belongs to another Operation")
        # Force repairs generated projection, never changes binding or authored drafts.
        selection = validate_workspace_selection(manifest, operation=operation)
        if isinstance(company_growth_periods, EllipsisType):
            company_growth_periods = cast("int | None", manifest.get("company_growth_periods", 12))
    else:
        selection = selection_binding(operation.payload)
        if selection is None:
            raise ResearchWorkspaceConflictError("Operation has no fixed selection")
    return _materialize(
        operation,
        selection,
        workspace,
        status="already_completed" if operation.status == "completed" else "resumed",
        force=force,
        company_growth_periods=company_growth_periods,
    )


def _materialize(
    operation: OperationSession,
    selection: ResearchSelectionRecord,
    workspace: Path,
    *,
    status: Literal["prepared", "resumed", "already_completed"],
    force: bool,
    company_growth_periods: int | EllipsisType | None,
) -> SelectedResearchPreparation:
    # 省略時は上で既存workspaceの表示を引き継ぎ、新規投影だけ12期から始める。
    # Noneは保存済み全期間の明示指定として区別する。
    if isinstance(company_growth_periods, EllipsisType):
        company_growth_periods = 12
    digest = selection_sha256(selection)
    projection = project_research_inputs(selection, company_growth_periods=company_growth_periods)
    purpose = "holding" if selection.request.scope == "review_positions" else "fundamental_research"
    manifest = {
        "schema_version": 2,
        "operation_id": operation.operation_id,
        "research_as_of": selection.research_as_of.isoformat(),
        "selection_sha256": digest,
        "purpose": purpose,
    }
    has_growth = (
        any(model.model_id == "company_growth" for model in selection.inputs.generation.models)
        if isinstance(selection, GeneratedSelection)
        else any("company_growth" in row for row in selection.inputs.securities_context.values())
    )
    if has_growth:
        manifest["company_growth_periods"] = company_growth_periods
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        inputs_path = workspace / "domain-inputs.yaml"
        if (
            inputs_path.exists()
            and not force
            and _load_mapping(inputs_path, label="domain inputs") != projection
        ):
            raise ResearchWorkspaceConflictError(
                "generated input drift; use --force to repair the projection"
            )
        _write_workspace_file(inputs_path, projection)
        _write_workspace_file(workspace / "manifest.yaml", manifest)
        for target in selection.targets:
            if target.purpose == "holding":
                child = workspace / "holdings" / target.ticker
                child.mkdir(parents=True, exist_ok=True)
                _write_workspace_file(
                    child / "manifest.yaml",
                    {
                        **manifest,
                        "holding_ticker": target.ticker,
                        "domain_inputs_ref": "../../domain-inputs.yaml",
                    },
                )
        if (
            _load_mapping(workspace / "manifest.yaml", label="manifest") != manifest
            or _load_mapping(inputs_path, label="domain inputs") != projection
        ):
            raise OSError("workspace projection readback failed")
    except (OSError, ValueError, ResearchWorkspaceError) as error:
        raise ResearchWorkspaceConflictError(
            f"Operation {operation.operation_id} is saved; resume its projection: {error}"
        ) from error
    return SelectedResearchPreparation(
        status=status,
        operation_id=operation.operation_id,
        workspace=workspace,
        scope=selection.request.scope,
        selected_tickers=tuple(t.ticker for t in selection.targets),
        selected_count=len(selection.targets),
        selection_sha256=digest,
        next_step=None if status == "already_completed" else "investment-judgment status",
    )
