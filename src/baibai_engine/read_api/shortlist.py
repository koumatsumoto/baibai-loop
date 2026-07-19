"""Query-only reviewed-shortlist views."""

from __future__ import annotations

import json
from pathlib import Path

from .sqlite import connect_read_only


def list_reviewed_shortlist_payloads(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    connection = connect_read_only(path)
    try:
        rows = connection.execute(
            "SELECT payload FROM reviewed_shortlist ORDER BY as_of DESC, published_at DESC"
        ).fetchall()
    finally:
        connection.close()
    result: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError("reviewed shortlist payload must be an object")
        result.append(payload)
    return result


__all__ = ["list_reviewed_shortlist_payloads"]
