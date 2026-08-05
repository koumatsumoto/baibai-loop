"""Query-only shortlist views."""

from __future__ import annotations

import json
from pathlib import Path

from .sqlite import read_rows

# Both queries must name the same newest shortlist, so they share one total order.
_SELECT = "SELECT payload FROM shortlist ORDER BY as_of DESC, published_at DESC, shortlist_id DESC"


def list_shortlist_payloads(path: Path) -> list[dict[str, object]]:
    return [_payload(row[0]) for row in read_rows(path, _SELECT)]


def latest_shortlist_payload(path: Path) -> dict[str, object] | None:
    """Return the shortlist `baibai-app` shows, without loading canonical history."""

    rows = read_rows(path, f"{_SELECT} LIMIT 1")
    return _payload(rows[0][0]) if rows else None


def _payload(raw: object) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("shortlist payload must be an object")
    return payload


__all__ = ["latest_shortlist_payload", "list_shortlist_payloads"]
