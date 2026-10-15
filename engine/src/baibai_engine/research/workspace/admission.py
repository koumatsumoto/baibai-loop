"""workspaceをOperationの固定選定へ束縛する。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from baibai_engine.research.operation.progress import selection_binding
from baibai_engine.research.operation.service import OperationService
from baibai_engine.research.selection import (
    ResearchSelection,
    ResearchTarget,
    selection_sha256,
    selection_targets,
)
from baibai_engine.research.workspace.files import (
    ResearchWorkspaceConflictError,
    _load_mapping,
    _nonempty_string,
)


def workspace_selection(
    manifest: Mapping[str, object], *, db_path: Path | None, active: bool = False
) -> ResearchSelection:
    expected_keys = {
        "schema_version",
        "operation_id",
        "research_as_of",
        "selection_sha256",
        "purpose",
    }
    if manifest.get("holding_ticker") is not None:
        expected_keys |= {"holding_ticker", "domain_inputs_ref"}
    if set(manifest) != expected_keys:
        raise ResearchWorkspaceConflictError("workspace manifest fields differ from V2")
    if manifest.get("schema_version") != 2:
        raise ResearchWorkspaceConflictError("workspace requires an explicit V2 cutover")
    identifier = _nonempty_string(manifest.get("operation_id"), label="manifest.operation_id")
    operation = OperationService(db_path).get(identifier)
    selection = selection_binding(operation.payload)
    if (
        selection is None
        or manifest.get("selection_sha256") != selection_sha256(selection)
        or str(manifest.get("research_as_of")) != operation.as_of.isoformat()
    ):
        raise ResearchWorkspaceConflictError(
            "workspace differs from the Operation's fixed selection"
        )
    purpose = "holding" if selection.request.scope == "review_positions" else "fundamental_research"
    if manifest.get("purpose") != purpose:
        raise ResearchWorkspaceConflictError("workspace purpose differs from the fixed selection")
    holding = manifest.get("holding_ticker")
    if holding is not None and ResearchTarget(
        ticker=str(holding), purpose="holding"
    ) not in selection_targets(selection):
        raise ResearchWorkspaceConflictError("holding subject is outside the fixed selection")
    if active and operation.status != "active":
        raise ResearchWorkspaceConflictError("completed research Operation cannot add work")
    return selection


def _require_primary_research_ticker(
    workspace: Path, ticker: str, *, action: str, gate: ResearchSelection, db_path: Path | None
) -> None:
    manifest = _load_mapping(workspace / "manifest.yaml", label="workspace manifest")
    selection = workspace_selection(manifest, db_path=db_path, active=True)
    purpose = "holding" if manifest.get("purpose") == "holding" else "new"
    if purpose == "holding" and manifest.get("holding_ticker") is None:
        raise ResearchWorkspaceConflictError(
            "author holding research in its holdings/<ticker> child workspace"
        )
    if (
        selection != gate
        or ResearchTarget(ticker=ticker, purpose=purpose) not in selection_targets(selection)
        or (manifest.get("holding_ticker") is not None and manifest["holding_ticker"] != ticker)
    ):
        raise ResearchWorkspaceConflictError(
            f"cannot {action}: ticker/purpose is outside the fixed selection"
        )
