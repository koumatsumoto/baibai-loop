"""L3 Researchの比較対象を止める境界: Operationに固定した人間の選択集合を照合する。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime

from .models import OperationPayload, OperationSession


def research_binding(payload: OperationPayload) -> tuple[str, frozenset[str]]:
    artifacts = [item for item in payload.artifacts if item.get("kind") == "research_triage"]
    if len(artifacts) != 1:
        raise ValueError("capital-allocation requires one research_triage Research Set binding")
    artifact = artifacts[0]
    reference, tickers = artifact.get("ref"), artifact.get("research_set")
    if (
        not isinstance(reference, str)
        or not reference
        or not isinstance(tickers, list)
        or not tickers
        or not all(isinstance(ticker, str) and ticker for ticker in tickers)
    ):
        raise ValueError("capital-allocation requires a non-empty Research Set binding")
    selected = frozenset(str(ticker) for ticker in tickers)
    if len(selected) != len(tickers):
        raise ValueError("Research Set binding has duplicate tickers")
    return reference, selected


def require_matching_research_set(
    operation: OperationSession,
    *,
    research_triage_id: str,
    tickers: Iterable[str],
    published_at: datetime,
) -> None:
    if operation.session_kind != "capital-allocation" or operation.status != "active":
        raise ValueError("an active capital-allocation Operation is required")
    triage_id, selected = research_binding(operation.payload)
    alternatives = tuple(tickers)
    if triage_id != research_triage_id:
        raise ValueError("assessment Research Triage differs from the active Operation")
    if frozenset(alternatives) != selected or len(alternatives) != len(selected):
        raise ValueError("assessment alternatives must exactly match the human Research Set")
    if published_at < operation.started_at:
        raise ValueError("assessment publication precedes the active Operation")


def require_active_research_set(
    connection: sqlite3.Connection,
    *,
    research_triage_id: str,
    tickers: Iterable[str],
    published_at: datetime,
) -> None:
    row = connection.execute("SELECT * FROM operation_session WHERE status = 'active'").fetchone()
    if row is None:
        raise ValueError("an active capital-allocation Operation is required")
    operation = OperationSession.model_validate(
        {**dict(row), "payload": json.loads(row["payload"])}
    )
    require_matching_research_set(
        operation,
        research_triage_id=research_triage_id,
        tickers=tickers,
        published_at=published_at,
    )
