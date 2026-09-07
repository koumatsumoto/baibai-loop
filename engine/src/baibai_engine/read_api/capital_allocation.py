"""Query-only capital-allocation-assessment views."""

from __future__ import annotations

import json
from pathlib import Path

from .research import reviewed_thesis_projection
from .sqlite import read_application_rows as read_rows


def list_capital_allocation_assessment_payloads(path: Path) -> list[dict[str, object]]:
    """Newest first, so the index reads as the current answer followed by history."""
    rows = read_rows(
        path,
        "SELECT payload FROM capital_allocation_assessment "
        "ORDER BY as_of DESC, published_at DESC, capital_allocation_assessment_id DESC",
    )
    return [_assessment_payload(row[0]) for row in rows]


def capital_allocation_assessment_payload(
    path: Path, *, capital_allocation_assessment_id: str
) -> dict[str, object] | None:
    rows = read_rows(
        path,
        "SELECT payload FROM capital_allocation_assessment "
        "WHERE capital_allocation_assessment_id = ?",
        (capital_allocation_assessment_id,),
    )
    return _payload(path, rows[0][0]) if rows else None


def _assessment_payload(raw: object) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("capital allocation assessment payload must be an object")
    version = payload.get("schema_version")
    if version != 1:
        raise ValueError(f"unsupported capital allocation schema_version: {version!r}")
    alternatives = payload.get("alternatives")
    if not isinstance(alternatives, list):
        raise ValueError("capital allocation alternatives must be a list")
    if any(not isinstance(item, dict) for item in alternatives):
        raise ValueError("capital allocation alternative must be an object")
    return payload


def _payload(path: Path, raw: object) -> dict[str, object]:
    payload = _assessment_payload(raw)
    alternatives = payload["alternatives"]
    assert isinstance(alternatives, list)
    projected: list[dict[str, object]] = []
    for raw_alternative in alternatives:
        if not isinstance(raw_alternative, dict):
            raise ValueError("capital allocation alternative must be an object")
        thesis_id = raw_alternative.get("thesis_id")
        thesis_hash = raw_alternative.get("thesis_core_sha256")
        rows = read_rows(
            path,
            "SELECT payload, core_sha256 FROM thesis WHERE thesis_id = ?",
            (thesis_id,),
        )
        if not rows:
            raise ValueError(f"allocation thesis is unavailable: {thesis_id}")
        if str(rows[0]["core_sha256"]) != thesis_hash:
            raise ValueError(f"allocation thesis binding has moved: {thesis_id}")
        projected.append(
            {
                **raw_alternative,
                "thesis_projection": reviewed_thesis_projection(path, thesis_id=str(thesis_id)),
            }
        )
    return {**payload, "alternatives": projected}


__all__ = ["capital_allocation_assessment_payload", "list_capital_allocation_assessment_payloads"]
