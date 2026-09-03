"""Query-only research_triage views."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.foundation.research_triage import ResearchTriage

from .sqlite import read_application_rows as read_rows

# Every global head consumer shares this real-instant total order. Raw ISO text order
# is not chronological when published_at values use different UTC offsets.
RESEARCH_TRIAGE_HEAD_ORDER = "as_of DESC, julianday(published_at) DESC, research_triage_id DESC"
# The interpolated order is a fixed module constant, never caller input.
_CURRENT_SCHEMA = 3
_SELECT = (  # nosec B608
    "SELECT payload FROM research_triage "
    f"WHERE json_extract(payload, '$.schema_version') = {_CURRENT_SCHEMA} "  # nosec B608
    f"ORDER BY {RESEARCH_TRIAGE_HEAD_ORDER}"
)
_SELECT_BY_REVIEW_SET = (
    "SELECT payload FROM research_triage WHERE review_set_id = ? "
    f"AND json_extract(payload, '$.schema_version') = {_CURRENT_SCHEMA} "
    f"ORDER BY {RESEARCH_TRIAGE_HEAD_ORDER}"  # nosec B608
)
_SELECT_BY_ID = (
    "SELECT payload FROM research_triage WHERE research_triage_id = ? "
    f"AND json_extract(payload, '$.schema_version') = {_CURRENT_SCHEMA}"  # nosec B608
)


@dataclass(frozen=True, slots=True)
class _ResearchTriageHead:
    research_triage_id: str
    as_of: date
    published_at: datetime


def _research_triage_head(connection: sqlite3.Connection) -> _ResearchTriageHead | None:
    """Read the global head inside the caller's transaction."""

    row = connection.execute(
        "SELECT research_triage_id, as_of, published_at FROM research_triage "
        f"WHERE json_extract(payload, '$.schema_version') = {_CURRENT_SCHEMA} "
        f"ORDER BY {RESEARCH_TRIAGE_HEAD_ORDER} LIMIT 1"  # nosec B608
    ).fetchone()
    if row is None:
        return None
    published_at = datetime.fromisoformat(str(row[2]))
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError("stored research triage published_at must include a timezone")
    return _ResearchTriageHead(
        research_triage_id=str(row[0]),
        as_of=date.fromisoformat(str(row[1])),
        published_at=published_at,
    )


def list_research_triage_payloads(path: Path) -> list[dict[str, object]]:
    return [_payload(row[0]) for row in read_rows(path, _SELECT)]


def research_triage_payloads_for_review_set(
    path: Path, review_set_id: str
) -> list[dict[str, object]]:
    """Return every current-contract judgment for one Review Set, newest first."""

    return [_payload(row[0]) for row in read_rows(path, _SELECT_BY_REVIEW_SET, (review_set_id,))]


def latest_research_triage_payload(path: Path) -> dict[str, object] | None:
    """Return the research_triage `baibai-web` shows, without loading canonical history."""

    rows = read_rows(path, f"{_SELECT} LIMIT 1")
    return _payload(rows[0][0]) if rows else None


def research_triage_payload(path: Path, research_triage_id: str) -> dict[str, object] | None:
    rows = read_rows(path, _SELECT_BY_ID, (research_triage_id,))
    return _payload(rows[0][0]) if rows else None


def current_research_triage(path: Path, research_triage_id: str) -> ResearchTriage | None:
    """Return one current-contract judgment."""

    payload = research_triage_payload(path, research_triage_id)
    return ResearchTriage.model_validate(payload) if payload is not None else None


def research_triage_payload_hash(triage: ResearchTriage) -> str:
    """Hash the canonical persisted current payload exactly once for workspace binding."""

    payload = canonical_json(triage.model_dump(mode="json"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _payload(raw: object) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("research_triage payload must be an object")
    if payload.get("schema_version") != _CURRENT_SCHEMA:
        raise ValueError(
            f"unsupported research triage schema_version: {payload.get('schema_version')!r}"
        )
    return payload


__all__ = [
    "RESEARCH_TRIAGE_HEAD_ORDER",
    "current_research_triage",
    "latest_research_triage_payload",
    "list_research_triage_payloads",
    "research_triage_payload",
    "research_triage_payload_hash",
    "research_triage_payloads_for_review_set",
]
