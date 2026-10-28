"""workspaceをOperationの固定選定へ束縛する。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    ResearchSelectionRecord as ResearchSelection,
)
from baibai_engine.investment_judgment.workflow.selection import (
    ResearchTarget,
    selection_sha256,
    selection_targets,
)
from baibai_engine.investment_judgment.workflow.workspace.files import (
    ResearchWorkspaceConflictError,
    _nonempty_string,
)
from baibai_engine.portfolio.ledger.events import replay_events_through
from baibai_engine.portfolio.ledger.store import LedgerStoreService


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
    if "company_growth_periods" in manifest:
        expected_keys.add("company_growth_periods")
        periods = manifest["company_growth_periods"]
        if periods is not None and (type(periods) is not int or periods < 1):
            raise ResearchWorkspaceConflictError("invalid company growth display periods")
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


def require_authoring_selection(
    manifest: Mapping[str, object], *, ticker: str, action: str, db_path: Path | None
) -> ResearchSelection:
    """Resolve authoring admission once; read-only status uses workspace_selection instead."""
    selection = workspace_selection(manifest, db_path=db_path, active=True)
    purpose = "holding" if manifest.get("purpose") == "holding" else "new"
    holding = manifest.get("holding_ticker")
    if purpose == "holding" and holding is None:
        raise ResearchWorkspaceConflictError(
            "author holding research in its holdings/<ticker> child workspace"
        )
    if ResearchTarget(ticker=ticker, purpose=purpose) not in selection_targets(selection) or (
        holding is not None and holding != ticker
    ):
        raise ResearchWorkspaceConflictError(
            f"cannot {action}: ticker/purpose is outside the fixed selection"
        )
    if holding is not None:
        ledger = LedgerStoreService(db_path).load()
        state = replay_events_through(ledger.events, ledger.as_of)
        if sum(lot.quantity for lot in state.lots.get(ticker, [])) <= 0:
            raise ResearchWorkspaceConflictError("holding workspace requires a current holding")
    return selection
