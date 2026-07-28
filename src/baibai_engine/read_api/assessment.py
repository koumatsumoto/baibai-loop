"""Query-only bargain-assessment views."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .sqlite import connect_read_only


def list_bargain_assessment_payloads(path: Path) -> list[dict[str, object]]:
    """Newest first, so the index reads as the current answer followed by history."""
    if not path.is_file():
        return []
    connection = connect_read_only(path)
    try:
        rows = connection.execute(
            "SELECT payload FROM bargain_assessment "
            "ORDER BY as_of DESC, published_at DESC, assessment_id DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    return [_payload(row[0]) for row in rows]


def bargain_assessment_payload(path: Path, *, assessment_id: str) -> dict[str, object] | None:
    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        row = connection.execute(
            "SELECT payload FROM bargain_assessment WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    return None if row is None else _payload(row[0])


def _payload(raw: object) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("bargain assessment payload must be an object")
    return payload


__all__ = ["bargain_assessment_payload", "list_bargain_assessment_payloads"]
