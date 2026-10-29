"""固定入力を一度保存し、再生成可能なworkspace投影を作る。"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import EllipsisType
from typing import Literal, cast

from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.market_assessment.query import latest_assessment
from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.models import (
    OperationPayload,
    OperationSession,
)
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.operation.validation import validate_target_purposes
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchInputs,
    GeneratedResearchSelection,
    GeneratedResearchSelectionDraft,
    ResearchScope,
    ResearchSelectionRecord,
    ResearchTarget,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.selection_policy import (
    CURRENT_RESEARCH_CAPACITY,
    validate_capacity,
)
from baibai_engine.investment_judgment.workflow.workspace.admission import workspace_selection
from baibai_engine.investment_judgment.workflow.workspace.files import (
    ResearchWorkspaceConflictError,
    _load_mapping,
    _write_workspace_file,
)
from baibai_engine.machine_analysis.generation.query import read_research_model_inputs
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only


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
        isinstance(selection, GeneratedResearchSelection)
        and selection.request == draft.request
        and selection.research_as_of == draft.research_as_of
        and selection.input_known_at == draft.input_known_at
        and selection.targets == draft.targets
        and selection.selection_summary == draft.selection_summary
        and selection.inputs.generation.generation_id == draft.generation_id
        and selection.inputs.screening_selection_id == draft.screening_selection_id
    )


def _generated_selection(
    draft: GeneratedResearchSelectionDraft, *, runs_path: Path, app_path: Path
) -> GeneratedResearchSelection:
    if draft.request.scope == "discover_new" and draft.screening_selection_id is None:
        raise ValueError("discovery requires an explicit saved screening selection")
    publication = None
    if draft.screening_selection_id is not None:
        publication = read_publication(path=runs_path, selection_id=draft.screening_selection_id)
        if publication is None or publication.generation.generation_id != draft.generation_id:
            raise ValueError("screening selection differs from the chosen generation")
    models = read_research_model_inputs(
        draft.generation_id, targets=tuple(t.ticker for t in draft.targets), path=runs_path
    )
    if models is None:
        raise ValueError("chosen model generation is unavailable")
    context = latest_assessment(app_path, as_of=draft.research_as_of, known_at=draft.input_known_at)
    return GeneratedResearchSelection(
        request=draft.request,
        research_as_of=draft.research_as_of,
        input_known_at=draft.input_known_at,
        selection_summary=draft.selection_summary,
        targets=draft.targets,
        capacity=CURRENT_RESEARCH_CAPACITY,
        inputs=GeneratedResearchInputs(
            generation=models,
            screening_selection_id=draft.screening_selection_id,
            screening_selection=None if publication is None else publication.selection,
            screening_published_at=None if publication is None else publication.published_at,
            saved_market_context_assessment=None
            if context is None
            else assessment_projection(context, as_of=draft.research_as_of),
        ),
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
    if now.utcoffset() is None or draft.input_known_at > now:
        raise ResearchWorkspaceConflictError("input_known_at is after prepare time")
    service = OperationService(db_path, runs_path=runs_path)
    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists():
        manifest = _load_mapping(manifest_path, label="workspace manifest")
        manifest_selection = workspace_selection(manifest, db_path=db_path)
        if not _same_draft(manifest_selection, draft):
            raise ResearchWorkspaceConflictError("workspace has a different fixed selection")
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
        validate_capacity(len(draft.targets), CURRENT_RESEARCH_CAPACITY)
    except ValueError as error:
        raise ResearchWorkspaceConflictError(str(error)) from error
    tickers = tuple(t.ticker for t in draft.targets)
    if database_path(db_path).exists():
        with closing(connect_read_only(database_path(db_path))) as connection:
            try:
                validate_target_purposes(
                    connection,
                    tuple(
                        ResearchTarget(ticker=t.ticker, purpose=t.purpose) for t in draft.targets
                    ),
                    now=now,
                )
            except ValueError as error:
                raise ResearchWorkspaceConflictError(str(error)) from error
    selection = _generated_selection(draft, runs_path=runs_path, app_path=database_path(db_path))
    holding = draft.request.scope == "review_positions"
    operation = service.start(
        session_kind="position-review" if holding else "capital-allocation",
        as_of=draft.research_as_of,
        started_at=now,
        ticker=tickers[0] if holding and len(tickers) == 1 else None,
        payload=OperationPayload(
            checkpoint="Research selection and fixed inputs saved",
            artifacts=(selection.model_dump(mode="json"),),
            canonical_refs=(draft.generation_id,),
            next="research the selected targets; record completed or unfinished outcomes",
        ),
    )
    committed = service.get(operation.operation_id)
    saved = selection_binding(committed.payload)
    assert saved is not None
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
    selection = selection_binding(operation.payload)
    if selection is None:
        raise ResearchWorkspaceConflictError("Operation has no fixed selection")
    if now.utcoffset() is None or now < operation.started_at:
        raise ResearchWorkspaceConflictError("resume precedes the Operation start")
    manifest_path = workspace / "manifest.yaml"
    if manifest_path.exists():
        manifest = _load_mapping(manifest_path, label="workspace manifest")
        if manifest.get("operation_id") != operation_id:
            raise ResearchWorkspaceConflictError("workspace belongs to another Operation")
        # Force repairs generated projection, never changes binding or authored drafts.
        workspace_selection(manifest, db_path=db_path)
        if isinstance(company_growth_periods, EllipsisType):
            company_growth_periods = cast("int | None", manifest.get("company_growth_periods", 12))
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
        if isinstance(selection, GeneratedResearchSelection)
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
    except (OSError, ResearchWorkspaceConflictError) as error:
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
