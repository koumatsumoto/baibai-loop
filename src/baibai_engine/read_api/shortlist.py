"""Query-only shortlist views."""

from __future__ import annotations

import json
from pathlib import Path

from .sqlite import connect_read_only


def list_shortlist_payloads(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    connection = connect_read_only(path)
    try:
        rows = connection.execute(
            "SELECT payload FROM shortlist ORDER BY as_of DESC, published_at DESC"
        ).fetchall()
    finally:
        connection.close()
    result: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError("shortlist payload must be an object")
        result.append(payload)
    return result


def latest_shortlist_payload(path: Path) -> dict[str, object] | None:
    """Return the current Baibai App shortlist without loading canonical history."""

    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        row = connection.execute(
            """
            SELECT payload FROM shortlist
            ORDER BY as_of DESC, published_at DESC, shortlist_id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    payload = json.loads(str(row[0]))
    if not isinstance(payload, dict):
        raise ValueError("shortlist payload must be an object")
    return payload


__all__ = ["latest_shortlist_payload", "list_shortlist_payloads"]
