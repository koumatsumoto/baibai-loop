"""Query-only macro context and indicator views."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .sqlite import connect_read_only


def latest_macro_context_payload(path: Path, *, as_of: date) -> dict[str, object] | None:
    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        row = connection.execute(
            """
            SELECT payload FROM macro_context
            WHERE as_of <= ?
            ORDER BY as_of DESC, published_at DESC, context_id DESC
            LIMIT 1
            """,
            (as_of.isoformat(),),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    payload = json.loads(str(row[0]))
    if not isinstance(payload, dict):
        raise ValueError("macro context payload must be an object")
    return payload


def list_macro_context_payloads(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    connection = connect_read_only(path)
    try:
        rows = connection.execute(
            "SELECT payload FROM macro_context "
            "ORDER BY as_of DESC, published_at DESC, context_id DESC"
        ).fetchall()
    finally:
        connection.close()
    result: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError("macro context payload must be an object")
        result.append(payload)
    return result


def macro_context_payload(
    path: Path,
    *,
    context_id: str,
    as_of: date,
) -> dict[str, object]:
    if not path.is_file():
        raise ValueError(f"application database not found: {path}")
    connection = connect_read_only(path)
    try:
        row = connection.execute(
            "SELECT as_of, payload FROM macro_context WHERE context_id = ?", (context_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError(f"unknown context_id: {context_id}")
    context_as_of = date.fromisoformat(str(row[0]))
    if context_as_of > as_of:
        raise ValueError(
            f"future macro context is not eligible: {context_id} as_of={context_as_of}"
        )
    payload = json.loads(str(row[1]))
    if not isinstance(payload, dict):
        raise ValueError("macro context payload must be an object")
    return payload


def macro_indicator_series(
    path: Path,
    *,
    series_id: str,
    limit: int = 36,
) -> dict[str, object] | None:
    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        series = connection.execute(
            "SELECT name, unit FROM series WHERE series_id = ?", (series_id,)
        ).fetchone()
        if series is None:
            return None
        rows = connection.execute(
            """
            SELECT observed_at, value, unit FROM (
                SELECT observed_at, value, unit,
                       row_number() OVER (
                           PARTITION BY observed_at ORDER BY vintage_at DESC
                       ) AS rank
                FROM observations
                WHERE series_id = ? AND fetch_status = 'ok'
            )
            WHERE rank = 1
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (series_id, limit),
        ).fetchall()
    finally:
        connection.close()
    points = [{"observed_at": str(row[0]), "value": float(row[1])} for row in reversed(rows)]
    return {
        "series_id": series_id,
        "name": str(series[0]),
        "unit": str(series[1]),
        "points": points,
    }


__all__ = [
    "latest_macro_context_payload",
    "list_macro_context_payloads",
    "macro_context_payload",
    "macro_indicator_series",
]
