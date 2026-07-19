"""Query-only operation session views."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

from .sqlite import connect_read_only

OperationStatus = Literal["active", "completed"]


def operation_session(path: Path, operation_id: str) -> dict[str, object] | None:
    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        row = connection.execute(
            "SELECT * FROM operation_session WHERE operation_id = ?", (operation_id,)
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else _public(row)


def list_operation_sessions(
    path: Path,
    *,
    status: OperationStatus | None = None,
) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    sql = "SELECT * FROM operation_session"
    parameters: tuple[object, ...] = ()
    if status is not None:
        sql += " WHERE status = ?"
        parameters = (status,)
    sql += " ORDER BY started_at DESC, operation_id DESC"
    connection = connect_read_only(path)
    try:
        rows = connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()
    return [_public(row) for row in rows]


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
