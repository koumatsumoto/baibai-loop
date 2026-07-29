"""Query-only operation session views."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

from .sqlite import read_rows

OperationStatus = Literal["active", "completed"]


def operation_session(path: Path, operation_id: str) -> dict[str, object] | None:
    rows = read_rows(
        path, "SELECT * FROM operation_session WHERE operation_id = ?", (operation_id,)
    )
    return _public(rows[0]) if rows else None


def list_operation_sessions(
    path: Path,
    *,
    status: OperationStatus | None = None,
) -> list[dict[str, object]]:
    sql = "SELECT * FROM operation_session"
    parameters: tuple[object, ...] = ()
    if status is not None:
        sql += " WHERE status = ?"
        parameters = (status,)
    sql += " ORDER BY started_at DESC, operation_id DESC"
    return [_public(row) for row in read_rows(path, sql, parameters)]


def _public(row: sqlite3.Row) -> dict[str, object]:
    return {
        "operation_id": row["operation_id"],
        "session_kind": row["session_kind"],
        "status": row["status"],
        "as_of": row["as_of"],
        "ticker": row["ticker"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "payload": json.loads(str(row["payload"])),
    }


__all__ = ["list_operation_sessions", "operation_session"]
