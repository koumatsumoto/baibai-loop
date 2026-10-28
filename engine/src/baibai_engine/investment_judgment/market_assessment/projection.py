"""保存本文からの小さな判断投影。過去のinput.jsonの再現ではない。"""

from __future__ import annotations

from datetime import date
from typing import Any

from baibai_engine.investment_judgment.market_assessment.models import ASSESSMENT_STALE_DAYS, StoredAssessment


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
            "synthesis": None,
        }
    body = record.document
    synthesis: dict[str, Any] | None = None
    if record.schema_version in (5, 6):
        changes = body["changes"]
        counter = body["counter_evidence"]
        uncertainty = body["uncertainties"]
        implications = body["implications"]
        sources = body["inputs"]
    else:
        # Historical report fields are quoted, never promoted into a new judgment.
        core = body.get("core", [])
        connection = body.get("connection", {})
        if body.get("synthesis"):
            synthesis = {
                "dominant_forces": [
                    {
                        key: force[key]
                        for key in (
                            "force_id",
                            "title",
                            "summary",
                            "transmission",
                            "direction",
                            "confidence",
                            "counter_evidence",
                            "source_ids",
                        )
                        if key in force
                    }
                    for force in body["synthesis"].get("dominant_forces", [])
                ],
                "interactions": [
                    {
                        key: item[key]
                        for key in ("summary", "force_ids", "source_ids")
                        if key in item
                    }
                    for item in body["synthesis"].get("interactions", [])
                ],
            }
        changes = [item for section in core for item in section.get("material_deltas", [])]
        counter = [
            {"summary": force["counter_evidence"], "source_ids": force.get("source_ids", [])}
            for force in (synthesis or {}).get("dominant_forces", [])
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
        "synthesis": synthesis,
    }
