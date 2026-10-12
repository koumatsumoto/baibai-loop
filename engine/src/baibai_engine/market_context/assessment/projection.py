"""保存本文からの小さな判断投影。過去のinput.jsonの再現ではない。"""

from __future__ import annotations

from datetime import date
from typing import Any

from .models import ASSESSMENT_STALE_DAYS, StoredAssessment


def assessment_projection(record: StoredAssessment | None, *, as_of: date) -> dict[str, Any]:
    if record is None:
        return {
            "status": "missing",
            "context_id": None,
            "summary": None,
            "changes": [],
            "counter_evidence": [],
            "uncertainties": [],
            "implications": [],
            "sources": [],
        }
    body = record.document
    if record.schema_version == 5:
        changes = body["changes"]
        counter = body["counter_evidence"]
        uncertainty = body["uncertainties"]
        implications = body["implications"]
        sources = body["inputs"]
    else:
        # Historical report fields are quoted, never promoted into a new judgment.
        core = body.get("core", [])
        connection = body.get("connection", {})
        changes = [item for section in core for item in section.get("material_deltas", [])]
        counter = [
            {"summary": force["counter_evidence"], "source_ids": force.get("source_ids", [])}
            for force in (body.get("synthesis") or {}).get("dominant_forces", [])
            if force.get("counter_evidence")
        ]
        counter.extend(
            {
                "summary": value,
                "source_ids": section.get("risk_environment", {}).get("source_ids", []),
            }
            for section in core
            if section.get("risk_environment")
            for value in section["risk_environment"].get("falsifiers", [])
        )
        uncertainty = connection.get("estimate_caveats", []) + connection.get("sizing_cautions", [])
        implications = connection.get("research_priority_hints", []) + connection.get(
            "sector_tilts", []
        )
        if connection.get("judgment"):
            implications = [connection["judgment"], *implications]
        if body.get("sections"):
            implications = body["sections"]
        source_fields = (
            "input_id",
            "provider",
            "series_id",
            "url",
            "published_at",
            "accessed_at",
            "observation_as_of",
            "rules_revision",
            "status",
            "context_id",
            "result_digest",
        )
        sources = [
            {key: item[key] for key in source_fields if key in item}
            for group in body.get("inputs", {}).values()
            if isinstance(group, list)
            for item in group
        ]
    age = (as_of - record.as_of).days
    return {
        "status": "stale" if age > ASSESSMENT_STALE_DAYS else "current",
        "context_id": record.context_id,
        "schema_version": record.schema_version,
        "as_of": record.as_of.isoformat(),
        "published_at": record.published_at.isoformat(),
        "age_days": age,
        "summary": record.summary,
        "changes": changes,
        "counter_evidence": counter,
        "uncertainties": uncertainty,
        "implications": implications,
        "sources": sources,
    }


def legacy_context_projection(record: StoredAssessment | None, *, as_of: date) -> dict[str, Any]:
    """Extract the six historical fields; this is not verification of a runner input.json."""
    if record is None:
        return {
            "status": "missing",
            "as_of": None,
            "age_days": None,
            "summary": None,
            "synthesis": None,
            "connection": None,
        }
    age = (as_of - record.as_of).days
    return {
        "status": "stale" if age > ASSESSMENT_STALE_DAYS else "current",
        "as_of": record.as_of.isoformat(),
        "age_days": age,
        "summary": record.summary,
        "synthesis": record.document.get("synthesis"),
        "connection": record.document.get("connection"),
    }


def assessment_series_ids(record: StoredAssessment) -> frozenset[str]:
    body = record.document
    if record.schema_version == 5:
        return frozenset(
            str(value) for item in body.get("inputs", []) for value in item.get("series_ids", [])
        )
    inputs = body.get("inputs", {})
    result = {str(item["series_id"]) for item in inputs.get("indicator_series", [])}
    result.update(
        str(value) for section in body.get("core", []) for value in section.get("series_ids", [])
    )
    return frozenset(result)
