"""workspaceをOperationの固定選定へ束縛する。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from baibai_engine.investment_judgment.workflow.operation.models import OperationSession
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
from baibai_engine.portfolio.ledger.read import load_ledger_in_transaction
from baibai_engine.portfolio.ledger.store import LedgerStoreService

# V2 pinned the older display, not just the Selection. A new V3 workspace leaves
# retired company-level TradingView out even when its Generation is older.
LEGACY_WORKSPACE_SCHEMA_VERSION = 2
WORKSPACE_SCHEMA_VERSION = 3


def read_workspace_operation(
    manifest: Mapping[str, object],
    *,
    db_path: Path | None,
    connection: sqlite3.Connection | None = None,
) -> OperationSession:
    return OperationService(db_path).get(_manifest_operation_id(manifest), connection=connection)


def workspace_selection(
    manifest: Mapping[str, object],
    *,
    db_path: Path | None,
    active: bool = False,
    connection: sqlite3.Connection | None = None,
) -> ResearchSelection:
    operation = read_workspace_operation(manifest, db_path=db_path, connection=connection)
    return validate_workspace_selection(manifest, operation=operation, active=active)


def _manifest_operation_id(manifest: Mapping[str, object]) -> str:
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
        raise ResearchWorkspaceConflictError("workspace manifest fields differ from the schema")
    version = manifest.get("schema_version")
    if type(version) is not int or version not in {
        LEGACY_WORKSPACE_SCHEMA_VERSION,
        WORKSPACE_SCHEMA_VERSION,
    }:
        raise ResearchWorkspaceConflictError("unsupported workspace schema version")
    return _nonempty_string(manifest.get("operation_id"), label="manifest.operation_id")


def validate_workspace_selection(
    manifest: Mapping[str, object], *, operation: OperationSession, active: bool = False
) -> ResearchSelection:
    """既読Operationへの束縛を検証し、全社入力を再読・再展開しない。"""
    if _manifest_operation_id(manifest) != operation.operation_id:
        raise ResearchWorkspaceConflictError("workspace belongs to another Operation")
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
    manifest: Mapping[str, object],
    *,
    ticker: str,
    action: str,
    db_path: Path | None = None,
    connection: sqlite3.Connection | None = None,
) -> ResearchSelection:
    """Use the same authoring rules for preflight and under the App publication lock."""
    selection = workspace_selection(manifest, db_path=db_path, active=True, connection=connection)
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
        if connection is None:
            ledger = LedgerStoreService(db_path).load()
        else:
            ledger, _ = load_ledger_in_transaction(connection)
        state = replay_events_through(ledger.events, ledger.as_of)
        if sum(lot.quantity for lot in state.lots.get(ticker, [])) <= 0:
            raise ResearchWorkspaceConflictError("holding workspace requires a current holding")
    return selection
