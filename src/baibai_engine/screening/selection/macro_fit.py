"""Macro context summary for selection (soft context, never a gate)."""

from __future__ import annotations

from datetime import date

from baibai_engine.macro.context import MacroContext, macro_context_diagnostics


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
    diagnostics = macro_context_diagnostics(macro_context, asof_date=asof_date)
    return {
        "context_id": macro_context.context_id,
        "as_of": macro_context.as_of.isoformat(),
        "age_days": diagnostics["age_days"],
        "failed_inputs": diagnostics["failed_inputs"],
        "material_deltas": diagnostics["material_deltas"],
        "sizing_cautions": diagnostics["sizing_cautions"],
        "research_questions": diagnostics["research_questions"],
        "refresh_triggers": diagnostics["refresh_triggers"],
        "warnings": diagnostics["warnings"],
    }
