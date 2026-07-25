"""Macro-context consumption: freshness policy and payload accessors.

Freshness lives here rather than in the document because the report does not declare
its own shelf life. A consumer decides what "too old" means for its own decision, so
the threshold below belongs to the reader (screening select), not to the author.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import ValidationError

from .models import MacroContextDocument

# The recommended writing rhythm is roughly monthly (the week after the US payroll
# report, and before a spot decision). A head report older than this has skipped more
# than one turn of that rhythm, which is worth a warning next to a selection — never a
# reason to stop producing candidates.
MACRO_CONTEXT_STALE_DAYS = 45


@dataclass(frozen=True, slots=True)
class MacroContext:
    context_id: str
    as_of: date
    payload: Mapping[str, Any]

    def age_days(self, asof_date: date) -> int:
        return (asof_date - self.as_of).days

    def is_stale_for(self, asof_date: date) -> bool:
        return self.age_days(asof_date) > MACRO_CONTEXT_STALE_DAYS


def macro_context_from_payload(payload: Mapping[str, Any], *, source: str) -> MacroContext:
    try:
        document = MacroContextDocument.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "root"
        raise ValueError(
            f"macro context schema invalid at {location}: {first['msg']}: {source}"
        ) from exc
    return MacroContext(
        context_id=document.context_id,
        as_of=document.as_of,
        payload=document.payload(),
    )


def macro_context_diagnostics(
    context: MacroContext,
    *,
    asof_date: date,
) -> dict[str, object]:
    warnings: list[str] = []
    if context.as_of > asof_date:
        warnings.append("macro_context_future")
    if context.is_stale_for(asof_date):
        warnings.append("macro_context_stale")
    # A report may declare an input it could not obtain. Declaring it honestly must not
    # make it invisible to the reader, or omitting it would be the easier path.
    failed_inputs = context_failed_inputs(context.payload)
    if failed_inputs:
        warnings.append("macro_context_failed_inputs")
    return {
        "context_id": context.context_id,
        "as_of": context.as_of.isoformat(),
        "age_days": context.age_days(asof_date),
        "future": context.as_of > asof_date,
        "stale": context.is_stale_for(asof_date),
        "failed_inputs": list(failed_inputs),
        "material_deltas": list(context_material_deltas(context.payload)),
        "sizing_cautions": list(context_sizing_cautions(context.payload)),
        "research_questions": list(context_research_questions(context.payload)),
        "refresh_triggers": list(context_refresh_triggers(context.payload)),
        "warnings": warnings,
    }


def context_failed_inputs(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Input ids the report itself declares as failed, across every input type."""

    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping):
        return ()
    return tuple(
        str(item["input_id"])
        for key in ("articles", "indicator_series", "reading_snapshots")
        for item in _mapping_sequence(inputs.get(key))
        if item.get("status") == "failed" and isinstance(item.get("input_id"), str)
    )


def context_material_deltas(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        item
        for section in _mapping_sequence(payload.get("core"))
        for item in _mapping_sequence(section.get("material_deltas"))
    )


def context_sizing_cautions(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return _mapping_sequence(_connection(payload).get("sizing_cautions"))


def context_research_questions(payload: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(item["summary"])
        for item in _mapping_sequence(_connection(payload).get("research_priority_hints"))
        if isinstance(item.get("summary"), str) and str(item["summary"]).strip()
    )


def context_refresh_triggers(payload: Mapping[str, Any]) -> tuple[str, ...]:
    monitoring = _core_section(payload, "monitoring")
    return tuple(
        str(item["condition"])
        for item in _mapping_sequence(monitoring.get("monitoring_points"))
        if isinstance(item.get("condition"), str) and str(item["condition"]).strip()
    )


def _connection(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    connection = payload.get("connection")
    return connection if isinstance(connection, Mapping) else {}


def _core_section(payload: Mapping[str, Any], section_id: str) -> Mapping[str, Any]:
    return next(
        (
            section
            for section in _mapping_sequence(payload.get("core"))
            if section.get("section_id") == section_id
        ),
        {},
    )


def _mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


__all__ = [
    "MACRO_CONTEXT_STALE_DAYS",
    "MacroContext",
    "context_failed_inputs",
    "context_material_deltas",
    "context_refresh_triggers",
    "context_research_questions",
    "context_sizing_cautions",
    "macro_context_diagnostics",
    "macro_context_from_payload",
]
