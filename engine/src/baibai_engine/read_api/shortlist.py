"""Query-only shortlist views."""

from __future__ import annotations

import json
from pathlib import Path

from .sqlite import read_application_rows as read_rows

# Both queries must name the same newest shortlist, so they share one total order.
_SELECT = "SELECT payload FROM shortlist ORDER BY as_of DESC, published_at DESC, shortlist_id DESC"
_SELECT_BY_SELECTION = (
    "SELECT payload FROM shortlist WHERE selection_id = ? "
    "ORDER BY published_at DESC, shortlist_id DESC"
)


def list_shortlist_payloads(path: Path) -> list[dict[str, object]]:
    return [_payload(row[0]) for row in read_rows(path, _SELECT)]


def shortlist_payloads_for_selection(path: Path, selection_id: str) -> list[dict[str, object]]:
    """Return every canonical judgment made over one selection, newest first.

    Publication allows only one — a second shortlist over the same selection carries
    a Review Basis that is stale by then — so a caller that needs *the* judgment for
    a cycle asks by selection and treats any other count as a store it must not
    interpret. Asking by selection rather than by name is what keeps the judgment
    anchored to the machine inputs instead of to whatever ID a caller supplies.
    """

    return [_payload(row[0]) for row in read_rows(path, _SELECT_BY_SELECTION, (selection_id,))]


def latest_shortlist_payload(path: Path) -> dict[str, object] | None:
    """Return the shortlist `baibai-web` shows, without loading canonical history."""

    rows = read_rows(path, f"{_SELECT} LIMIT 1")
    return _payload(rows[0][0]) if rows else None


def _payload(raw: object) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("shortlist payload must be an object")
    if payload.get("schema_version") != 7:
        raise ValueError(f"unsupported shortlist schema_version: {payload.get('schema_version')!r}")
    return payload


__all__ = [
    "latest_shortlist_payload",
    "list_shortlist_payloads",
    "shortlist_payloads_for_selection",
]
