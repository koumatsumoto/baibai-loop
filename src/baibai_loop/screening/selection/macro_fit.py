"""Macro context fit diagnostics for candidates (soft lens, never a gate)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from baibai_loop.foundation.coerce import dict_sequence, string_or_none, string_sequence
from baibai_loop.macro.context import MacroContext, macro_context_diagnostics

from .records import CandidateRecord


def _candidate_macro_context_result(
    item: CandidateRecord,
    *,
    macro_context: MacroContext | None,
    asof_date: date,
) -> dict[str, object]:
    if macro_context is None:
        return {
            "context_id": None,
            "stale": False,
            "matched_items": [],
            "unknown_items": [],
            "warnings": ["macro_context_missing"],
        }
    return macro_context_diagnostics(
        macro_context,
        asof_date=asof_date,
        candidate_sector=item.sector_33,
    )


def _macro_context_summary(macro_context: MacroContext | None) -> dict[str, object] | None:
    if macro_context is None:
        return None
    payload = macro_context.payload
    return {
        "context_id": macro_context.context_id,
        "as_of": macro_context.as_of.isoformat(),
        "valid_until": macro_context.valid_until.isoformat(),
        "research_questions": string_sequence(payload.get("research_questions")),
        "refresh_triggers": string_sequence(payload.get("refresh_triggers")),
    }


def _macro_context_alignment(result: Mapping[str, object]) -> str:
    items = dict_sequence(result.get("matched_items"))
    stances = {string_or_none(item.get("stance")) for item in items}
    if stances & {"tailwind"}:
        return "tailwind"
    if stances & {"headwind"}:
        return "headwind"
    if stances & {"mixed"}:
        return "mixed"
    if stances & {"neutral"}:
        return "neutral"
    return "not_matched"


def _macro_rank(status: str | None) -> int:
    match status:
        case "tailwind":
            return 0
        case "neutral" | "mixed" | "not_matched":
            return 1
        case "headwind":
            return 2
        case _:
            return 3
