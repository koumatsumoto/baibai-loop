"""Read-only operational task queries."""

from __future__ import annotations

import json
from pathlib import Path

from .sqlite import read_rows


def task_store_exists(path: Path) -> bool:
    """Tell "no task store here" from "a store with no open task in it".

    The Dashboard says nothing about tasks when the store is absent and says
    "no open task" when it is present but empty, so the two must stay distinct.
    """

    rows = read_rows(path, "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='task'")
    return bool(rows)


def list_task_payloads(path: Path) -> list[dict[str, object]]:
    rows = read_rows(
        path,
        "SELECT payload FROM task "
        "ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, due_date, task_id",
    )
    payloads: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError("task payload must be an object")
        payloads.append(payload)
    return payloads


__all__ = ["list_task_payloads", "task_store_exists"]
