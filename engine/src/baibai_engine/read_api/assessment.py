"""Query-only bargain-assessment views."""

from __future__ import annotations

import json
from pathlib import Path

from .sqlite import read_rows


def list_bargain_assessment_payloads(path: Path) -> list[dict[str, object]]:
    """Newest first, so the index reads as the current answer followed by history."""
    rows = read_rows(
        path,
        "SELECT payload FROM bargain_assessment "
        "ORDER BY as_of DESC, published_at DESC, assessment_id DESC",
    )
    return [_payload(row[0]) for row in rows]


def bargain_assessment_payload(path: Path, *, assessment_id: str) -> dict[str, object] | None:
    rows = read_rows(
        path,
        "SELECT payload FROM bargain_assessment WHERE assessment_id = ?",
        (assessment_id,),
    )
    return _payload(rows[0][0]) if rows else None


def _payload(raw: object) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("bargain assessment payload must be an object")
    version = payload.get("schema_version")
    if version != 4:
        raise ValueError(f"unsupported bargain assessment schema_version: {version!r}")
    return payload


__all__ = ["bargain_assessment_payload", "list_bargain_assessment_payloads"]
