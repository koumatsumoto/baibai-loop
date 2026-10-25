"""発行時だけOperationとProposalの固定範囲を厳密照合する。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from baibai_engine.investment_judgment.operation.models import OperationSession
from baibai_engine.investment_judgment.operation.progress import proposal_scope, selection_binding
from baibai_engine.investment_judgment.selection import ResearchSelectionRecord as ResearchSelection
from baibai_engine.investment_judgment.trade_proposal import ProposalResearchScope, RequestScope


def require_proposal_research_scope(
    connection: sqlite3.Connection,
    *,
    scope: ProposalResearchScope,
    request_scope: RequestScope,
    published_at: datetime,
) -> ResearchSelection:
    """通常の保存read/previewはこの現在progress照合を呼ばない。"""
    row = connection.execute(
        "SELECT * FROM operation_session WHERE operation_id=?", (scope.operation_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Proposal requires its current Research Operation")
    operation = OperationSession.model_validate(
        {**dict(row), "payload": json.loads(row["payload"])}
    )
    if operation.status != "active":
        raise ValueError("Proposal publication requires an active Research Operation")
    selection = selection_binding(operation.payload)
    if selection is None:
        raise ValueError("fixed research_selection is required")
    if selection.request.scope != request_scope:
        raise ValueError("Proposal request scope differs from the Research selection")
    if proposal_scope(operation.operation_id, operation.payload) != scope:
        raise ValueError("Proposal scope differs from the Operation's fixed coverage")
    if published_at.utcoffset() is None or published_at < operation.started_at:
        raise ValueError("Proposal publication precedes the active Research Operation")
    return selection
