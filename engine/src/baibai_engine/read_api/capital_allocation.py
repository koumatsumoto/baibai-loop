"""Query-only capital-allocation-assessment views."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from baibai_engine.research.thesis import (
    ThesisDocument,
    UnpublishedThesis,
    evaluate_thesis,
)

from .sqlite import read_application_rows as read_rows


def list_capital_allocation_assessment_payloads(path: Path) -> list[dict[str, object]]:
    """Newest first, so the index reads as the current answer followed by history."""
    rows = read_rows(
        path,
        "SELECT payload FROM capital_allocation_assessment "
        "ORDER BY as_of DESC, published_at DESC, capital_allocation_assessment_id DESC",
    )
    return [_payload(path, row[0]) for row in rows]


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


def _payload(path: Path, raw: object) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("capital allocation assessment payload must be an object")
    version = payload.get("schema_version")
    if version != 1:
        raise ValueError(f"unsupported capital allocation schema_version: {version!r}")
    alternatives = payload.get("alternatives")
    if not isinstance(alternatives, list):
        raise ValueError("capital allocation alternatives must be a list")
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
        document = ThesisDocument.model_validate(json.loads(str(rows[0]["payload"])))
        projected.append({**raw_alternative, "thesis_projection": _thesis_projection(document)})
    return {**payload, "alternatives": projected}


def _thesis_projection(document: ThesisDocument) -> dict[str, object]:
    result = evaluate_thesis(document, identity=UnpublishedThesis.DRAFT)
    base = next(
        (
            scenario
            for scenario in result.scenarios
            if scenario.horizon_years == 5 and scenario.name == "base"
        ),
        None,
    )
    fair_value = _number(document.estimates.current_fair_value_yen)
    entry_price = _number(document.estimates.entry_price_basis_yen)
    return {
        "five_year_base_cagr_pct": None if base is None else round(base.total_return_cagr_pct, 4),
        "fair_value_yen": fair_value,
        "fv_gap_pct": (
            None
            if fair_value is None or entry_price in (None, 0)
            else round((fair_value / entry_price - 1) * 100, 4)
        ),
        "permanent_loss_conclusion": document.judgment.permanent_loss_conclusion,
    }


def _number(value: object) -> float | None:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


__all__ = ["capital_allocation_assessment_payload", "list_capital_allocation_assessment_payloads"]
