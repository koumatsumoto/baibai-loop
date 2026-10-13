"""L3 Researchの比較対象を止める境界: Operationに固定した対象集合を照合する。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime

from baibai_engine.research.operation.models import OperationSession
from baibai_engine.research.operation.progress import (
    ResearchComparisonScope,
    ReviewedTarget,
    comparison_scope,
    selection_binding,
)


def require_matching_research_set(
    operation: OperationSession,
    *,
    research_triage_id: str,
    tickers: Iterable[str],
    published_at: datetime,
    scope: ResearchComparisonScope | None = None,
) -> ResearchComparisonScope | None:
    if operation.session_kind != "capital-allocation" or operation.status != "active":
        raise ValueError("an active capital-allocation Operation is required")
    if selection_binding(operation.payload) is not None:
        expected = comparison_scope(operation.operation_id, operation.payload)
        selection = selection_binding(operation.payload)
        assert selection is not None
        if selection.research_triage_id != research_triage_id:
            raise ValueError("assessment Research Triage differs from the active Operation")
        completed = [item.ticker for item in expected.outcomes if isinstance(item, ReviewedTarget)]
        alternatives = tuple(tickers)
        if (
            not completed
            or frozenset(alternatives) != frozenset(completed)
            or len(alternatives) != len(completed)
        ):
            raise ValueError("assessment alternatives must exactly match completed new research")
        if scope is not None and scope != expected:
            raise ValueError("assessment scope differs from the Operation's fixed coverage")
        if published_at < operation.started_at:
            raise ValueError("assessment publication precedes the active Operation")
        return expected
    raise ValueError("explicit fixed research_selection is required before publishing CAA")


def require_active_research_set(
    connection: sqlite3.Connection,
    *,
    research_triage_id: str,
    tickers: Iterable[str],
    published_at: datetime,
    scope: ResearchComparisonScope | None = None,
) -> ResearchComparisonScope | None:
    row = connection.execute("SELECT * FROM operation_session WHERE status = 'active'").fetchone()
    if row is None:
        raise ValueError("an active capital-allocation Operation is required")
    operation = OperationSession.model_validate(
        {**dict(row), "payload": json.loads(row["payload"])}
    )
    return require_matching_research_set(
        operation,
        research_triage_id=research_triage_id,
        tickers=tickers,
        published_at=published_at,
        scope=scope,
    )
