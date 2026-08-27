"""Macro context summary for selection (soft context, never a gate)."""

from __future__ import annotations

from datetime import date

from baibai_engine.macro.context import MacroContext
from baibai_engine.macro.context.models import MACRO_CONTEXT_STALE_DAYS


def macro_context_summary(
    macro_context: MacroContext | None, *, asof_date: date
) -> dict[str, object]:
    """Summarise the head report for a selection, with the same keys either way.

    A missing context is a normal state, so the shape does not change with it: a
    consumer must not have to branch on key presence to read the summary.
    """
    if macro_context is None:
        return {
            "context_id": None,
            "as_of": None,
            "age_days": None,
            "failed_inputs": [],
            "material_deltas": [],
            "sizing_cautions": [],
            "research_questions": [],
            "refresh_triggers": [],
            "warnings": ["macro_context_missing"],
        }
    document = macro_context.document
    age_days = (asof_date - document.as_of).days
    inputs = (
        *document.inputs.articles,
        *document.inputs.indicator_series,
        *document.inputs.reading_snapshots,
        *document.inputs.machine_snapshots,
    )
    failed_inputs = [item.input_id for item in inputs if item.status == "failed"]
    warnings: list[str] = []
    if document.as_of > asof_date:
        warnings.append("macro_context_future")
    if age_days > MACRO_CONTEXT_STALE_DAYS:
        warnings.append("macro_context_stale")
    if failed_inputs:
        warnings.append("macro_context_failed_inputs")
    return {
        "context_id": document.context_id,
        "as_of": document.as_of.isoformat(),
        "age_days": age_days,
        "failed_inputs": failed_inputs,
        "material_deltas": [item.model_dump(mode="json") for item in document.material_deltas],
        "sizing_cautions": [item.model_dump(mode="json") for item in document.sizing_cautions],
        "research_questions": list(document.research_questions),
        "refresh_triggers": list(document.refresh_triggers),
        "warnings": warnings,
    }
