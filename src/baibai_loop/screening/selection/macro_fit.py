"""Macro context summary for selection (soft context, never a gate)."""

from __future__ import annotations

from datetime import date

from baibai_loop.foundation.coerce import string_sequence
from baibai_loop.macro.context import MacroContext, macro_context_diagnostics


def macro_context_summary(
    macro_context: MacroContext | None, *, asof_date: date
) -> dict[str, object]:
    if macro_context is None:
        return {
            "context_id": None,
            "as_of": None,
            "valid_until": None,
            "material_deltas": [],
            "sizing_cautions": [],
            "warnings": ["macro_context_missing"],
        }
    payload = macro_context.payload
    diagnostics = macro_context_diagnostics(macro_context, asof_date=asof_date)
    return {
        "context_id": macro_context.context_id,
        "as_of": macro_context.as_of.isoformat(),
        "valid_until": macro_context.valid_until.isoformat(),
        "material_deltas": diagnostics["material_deltas"],
        "sizing_cautions": diagnostics["sizing_cautions"],
        "warnings": diagnostics["warnings"],
        "research_questions": string_sequence(payload.get("research_questions")),
        "refresh_triggers": string_sequence(payload.get("refresh_triggers")),
    }
