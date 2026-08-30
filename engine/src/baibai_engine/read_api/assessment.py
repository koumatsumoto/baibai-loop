"""Query-only bargain-assessment views."""

from __future__ import annotations

import json
from pathlib import Path

from baibai_engine.research.assessment import derive_case_machine_values
from baibai_engine.research.thesis import ThesisDocument

from .sqlite import read_application_rows as read_rows


def list_bargain_assessment_payloads(path: Path) -> list[dict[str, object]]:
    """Newest first, so the index reads as the current answer followed by history."""
    rows = read_rows(
        path,
        "SELECT payload FROM bargain_assessment "
        "ORDER BY as_of DESC, published_at DESC, assessment_id DESC",
    )
    return [_payload(path, row[0]) for row in rows]


def bargain_assessment_payload(path: Path, *, assessment_id: str) -> dict[str, object] | None:
    rows = read_rows(
        path,
        "SELECT payload FROM bargain_assessment WHERE assessment_id = ?",
        (assessment_id,),
    )
    return _payload(path, rows[0][0]) if rows else None


def _payload(path: Path, raw: object) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("bargain assessment payload must be an object")
    version = payload.get("schema_version")
    if version != 5:
        raise ValueError(f"unsupported bargain assessment schema_version: {version!r}")
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("bargain assessment cases must be a list")
    projected_cases: list[dict[str, object]] = []
    for raw_case in cases:
        if not isinstance(raw_case, dict):
            raise ValueError("bargain assessment case must be an object")
        thesis_id = raw_case.get("thesis_id")
        thesis_hash = raw_case.get("thesis_core_sha256")
        rows = read_rows(
            path,
            "SELECT payload, core_sha256 FROM thesis WHERE thesis_id = ?",
            (thesis_id,),
        )
        if not rows:
            raise ValueError(f"assessment thesis is unavailable: {thesis_id}")
        if str(rows[0]["core_sha256"]) != thesis_hash:
            raise ValueError(f"assessment thesis binding has moved: {thesis_id}")
        document = ThesisDocument.model_validate(json.loads(str(rows[0]["payload"])))
        projected_cases.append({**raw_case, "machine": derive_case_machine_values(document)})
    return {**payload, "cases": projected_cases}


__all__ = ["bargain_assessment_payload", "list_bargain_assessment_payloads"]
