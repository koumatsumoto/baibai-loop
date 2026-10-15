"""Query-only operation session views."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from .sqlite import read_application_rows as read_rows

OperationStatus = Literal["active", "completed"]


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
    rows = sorted(
        read_rows(path, sql, parameters),
        key=lambda row: (datetime.fromisoformat(row["started_at"]), row["operation_id"]),
        reverse=True,
    )
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


__all__ = ["list_operation_sessions"]
