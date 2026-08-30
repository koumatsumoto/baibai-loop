"""Query-only research_triage views."""

from __future__ import annotations

import json
from pathlib import Path

from .sqlite import read_application_rows as read_rows

# Both queries must name the same newest research_triage, so they share one total order.
_SELECT = (
    "SELECT payload FROM research_triage "
    "ORDER BY as_of DESC, published_at DESC, research_triage_id DESC"
)
_SELECT_BY_REVIEW_SET = (
    "SELECT payload FROM research_triage WHERE review_set_id = ? "
    "ORDER BY published_at DESC, research_triage_id DESC"
)


def list_research_triage_payloads(path: Path) -> list[dict[str, object]]:
    return [_payload(row[0]) for row in read_rows(path, _SELECT)]


def research_triage_payloads_for_review_set(
    path: Path, review_set_id: str
) -> list[dict[str, object]]:
    """Return every canonical judgment for one Review Set, newest first.

    Publication allows only one current judgment because a second publication
    carries a stale Review Basis. Looking up by Review Set keeps the judgment
    anchored to its machine input.
    """

    return [_payload(row[0]) for row in read_rows(path, _SELECT_BY_REVIEW_SET, (review_set_id,))]


def latest_research_triage_payload(path: Path) -> dict[str, object] | None:
    """Return the research_triage `baibai-web` shows, without loading canonical history."""

    rows = read_rows(path, f"{_SELECT} LIMIT 1")
    return _payload(rows[0][0]) if rows else None


def _payload(raw: object) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("research_triage payload must be an object")
    if payload.get("schema_version") != 1:
        raise ValueError(
            f"unsupported research triage schema_version: {payload.get('schema_version')!r}"
        )
    return payload


__all__ = [
    "latest_research_triage_payload",
    "list_research_triage_payloads",
    "research_triage_payloads_for_review_set",
]
