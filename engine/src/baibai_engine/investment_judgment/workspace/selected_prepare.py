"""固定入力を一度保存し、再生成可能なworkspace投影を作る。"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from baibai_engine.investment_judgment.inputs import (
    collect_research_inputs,
    project_research_inputs,
)
from baibai_engine.investment_judgment.market_context.projection import assessment_projection
from baibai_engine.investment_judgment.market_context.query import latest_assessment
from baibai_engine.investment_judgment.operation.models import OperationPayload, OperationSession
from baibai_engine.investment_judgment.operation.progress import selection_binding
from baibai_engine.investment_judgment.operation.service import OperationService
from baibai_engine.investment_judgment.operation.validation import validate_target_purposes
from baibai_engine.investment_judgment.selection import (
    GeneratedResearchInputs,
    GeneratedResearchSelection,
    GeneratedResearchSelectionDraft,
    ResearchScope,
    ResearchSelection,
    ResearchSelectionDraft,
    ResearchSelectionRecord,
    ResearchTarget,
    selection_sha256,
)
from baibai_engine.investment_judgment.selection_policy import (
    CURRENT_RESEARCH_CAPACITY,
    validate_capacity,
)
from baibai_engine.investment_judgment.selection_query import load_analysis_snapshot
from baibai_engine.investment_judgment.workspace.admission import workspace_selection
from baibai_engine.investment_judgment.workspace.files import (
    ResearchWorkspaceConflictError,
    _load_mapping,
    _write_workspace_file,
)
from baibai_engine.machine_analysis.generation_query import read_model_inputs
from baibai_engine.machine_analysis.screening.selection_query import read_selection
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only


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


def _growth_range_matches(value: object, draft: ResearchSelectionDraft) -> bool:
    if not isinstance(value, dict):
        return draft.company_growth_start is None and draft.company_growth_end is None
    return (
        value.get("requested_start")
        == (draft.company_growth_start.isoformat() if draft.company_growth_start else None)
        and value.get("requested_end")
        == (draft.company_growth_end or draft.research_as_of).isoformat()
    )


def _same_draft(
    selection: ResearchSelectionRecord,
    draft: ResearchSelectionDraft | GeneratedResearchSelectionDraft,
) -> bool:
    common = (
        selection.request == draft.request
        and selection.research_as_of == draft.research_as_of
        and selection.input_known_at == draft.input_known_at
        and selection.targets == draft.targets
        and selection.selection_summary == draft.selection_summary
    )
    if isinstance(selection, GeneratedResearchSelection):
        return (
            common
            and isinstance(draft, GeneratedResearchSelectionDraft)
            and selection.inputs.generation.generation_id == draft.generation_id
            and selection.inputs.screening_selection_id == draft.screening_selection_id
        )
    return (
        common
        and isinstance(draft, ResearchSelectionDraft)
        and selection.inputs.analysis.run_revision_id == draft.run_revision_id
        and selection.inputs.input_gaps == draft.input_gaps
        and all(
            _growth_range_matches(row.get("company_growth"), draft)
            for row in selection.inputs.securities_context.values()
        )
    )


def _generated_selection(
    draft: GeneratedResearchSelectionDraft, *, runs_path: Path, app_path: Path
) -> GeneratedResearchSelection:
    if draft.request.scope == "discover_new" and draft.screening_selection_id is None:
        raise ValueError("discovery requires an explicit saved screening selection")
    screening = None
    if draft.screening_selection_id is not None:
        screening = read_selection(draft.screening_selection_id, path=runs_path)
        if screening is None or screening.generation_id != draft.generation_id:
            raise ValueError("screening selection differs from the chosen generation")
    models = read_model_inputs(
        draft.generation_id, subjects=("market", *(t.ticker for t in draft.targets)), path=runs_path
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
            screening_selection=screening,
            saved_market_context_assessment=None
            if context is None
            else assessment_projection(context, as_of=draft.research_as_of),
        ),
    )


def prepare_selected_workspace(
    *,
    draft: ResearchSelectionDraft | GeneratedResearchSelectionDraft,
    db_path: Path | None,
    workspace: Path,
    sqlite_path: Path,
    runs_path: Path,
    indicators_path: Path,
    now: datetime,
    force: bool = False,
    company_growth_mirror: Path = Path("stores"),
    company_growth_periods: int | None = 12,
) -> SelectedResearchPreparation:
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
    selection: ResearchSelectionRecord
    canonical_refs: tuple[str, ...]
    if isinstance(draft, GeneratedResearchSelectionDraft):
        selection = _generated_selection(
            draft, runs_path=runs_path, app_path=database_path(db_path)
        )
        canonical_refs = (draft.generation_id,)
    else:
        snapshot = load_analysis_snapshot(
            run_revision_id=draft.run_revision_id,
            research_as_of=draft.research_as_of,
            tickers=tickers,
            gaps=draft.input_gaps,
            known_at=draft.input_known_at,
            runs_path=runs_path,
        )
        inputs = collect_research_inputs(
            snapshot=snapshot,
            input_gaps=draft.input_gaps,
            tickers=tickers,
            research_as_of=draft.research_as_of,
            input_known_at=draft.input_known_at,
            market_path=sqlite_path,
            indicators_path=indicators_path,
            app_path=database_path(db_path),
            company_growth_mirror=company_growth_mirror,
            company_growth_start=draft.company_growth_start,
            company_growth_end=draft.company_growth_end,
        )
        selection = ResearchSelection(
            request=draft.request,
            research_as_of=draft.research_as_of,
            input_known_at=draft.input_known_at,
            selection_summary=draft.selection_summary,
            targets=draft.targets,
            capacity=CURRENT_RESEARCH_CAPACITY,
            inputs=inputs,
        )
        canonical_refs = () if snapshot.run_revision_id is None else (snapshot.run_revision_id,)
    holding = draft.request.scope == "review_positions"
    operation = service.start(
        session_kind="position-review" if holding else "capital-allocation",
        as_of=draft.research_as_of,
        started_at=now,
        ticker=tickers[0] if holding and len(tickers) == 1 else None,
        payload=OperationPayload(
            checkpoint="Research selection and fixed inputs saved",
            artifacts=(selection.model_dump(mode="json"),),
            canonical_refs=canonical_refs,
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
    company_growth_periods: int | None = 12,
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
    company_growth_periods: int | None,
) -> SelectedResearchPreparation:
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
