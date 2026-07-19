"""Read-only operational task queries."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .sqlite import connect_read_only


def task_store_exists(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with closing(connect_read_only(path)) as connection:
            return bool(
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='task'"
                ).fetchone()
            )
    except sqlite3.Error:
        return False


def list_task_payloads(path: Path) -> list[dict[str, Any]]:
    if not task_store_exists(path):
        return []
    with closing(connect_read_only(path)) as connection:
        rows = connection.execute(
            "SELECT payload FROM task "
            "ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, due_date, task_id"
        ).fetchall()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError("task payload must be an object")
        payloads.append(payload)
    return payloads


__all__ = ["list_task_payloads", "task_store_exists"]
