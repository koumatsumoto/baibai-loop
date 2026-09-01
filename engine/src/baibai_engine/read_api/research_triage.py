"""Query-only research_triage views."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.foundation.research_triage import ResearchTriage

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
_SELECT_BY_ID = "SELECT payload FROM research_triage WHERE research_triage_id = ?"


def list_research_triage_payloads(path: Path) -> list[dict[str, object]]:
    return [_payload(row[0]) for row in read_rows(path, _SELECT)]


def research_triage_payloads_for_review_set(
    path: Path, review_set_id: str
) -> list[dict[str, object]]:
    """Return every canonical judgment for one Review Set, newest first.

    Historical views may include more than one judgment for a Review Set. The
    ordered projection lets read models choose the newest while preserving every
    immutable publication.
    """

    return [_payload(row[0]) for row in read_rows(path, _SELECT_BY_REVIEW_SET, (review_set_id,))]


def latest_research_triage_payload(path: Path) -> dict[str, object] | None:
    """Return the research_triage `baibai-web` shows, without loading canonical history."""

    rows = read_rows(path, f"{_SELECT} LIMIT 1")
    return _payload(rows[0][0]) if rows else None


def research_triage_payload(path: Path, research_triage_id: str) -> dict[str, object] | None:
    rows = read_rows(path, _SELECT_BY_ID, (research_triage_id,))
    return _payload(rows[0][0]) if rows else None


def current_research_triage(path: Path, research_triage_id: str) -> ResearchTriage | None:
    """Return one current v2 judgment; historical v1 remains a read-model concern."""

    payload = research_triage_payload(path, research_triage_id)
    return ResearchTriage.model_validate(payload) if payload is not None else None


def research_triage_payload_hash(triage: ResearchTriage) -> str:
    """Hash the canonical persisted v2 payload exactly once for workspace binding."""

    payload = canonical_json(triage.model_dump(mode="json"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _payload(raw: object) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("research_triage payload must be an object")
    if payload.get("schema_version") not in {1, 2}:
        raise ValueError(
            f"unsupported research triage schema_version: {payload.get('schema_version')!r}"
        )
    return payload


__all__ = [
    "current_research_triage",
    "latest_research_triage_payload",
    "list_research_triage_payloads",
    "research_triage_payload",
    "research_triage_payload_hash",
    "research_triage_payloads_for_review_set",
]
