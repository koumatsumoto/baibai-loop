"""Shared immutable financial input selections; source bodies remain in L1."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.full_day_inputs import (
    FullDayInputContract,
    read_full_day_input,
    retain_full_day_input,
)
from baibai_engine.machine_analysis.generation.native_input import (
    VerifiedNativeInput,
    rehydrate_native_input,
)
from baibai_engine.storage.objects.r2 import ObjectStore

INPUT_FORMAT = "jquants-financial-input-v2"
INPUT_PREFIX = "fixed-inputs/jquants-financial/v1/"
FINANCIAL_PROJECTION_REVISION = "jquants-financial-projection-v1"
_CONTRACT = FullDayInputContract(
    INPUT_PREFIX,
    INPUT_FORMAT,
    "jquants-financial-selection-v1",
    "jquants-financial-month-v1",
    allow_legacy_inline=True,
)


class SavedFinancialProjectionRevisionMismatch(ValueError):
    """The raw membership alone cannot reproduce a finalized financial projection."""


def rehydrate_financial_input(saved: InputSnapshot | bytes) -> VerifiedNativeInput:
    """Validate the finalized source and projection recipe before any object read."""
    fixed = rehydrate_native_input(saved, source="financial")
    descriptor = fixed.descriptor
    if "format" in descriptor and descriptor["format"] != INPUT_FORMAT:
        raise ValueError("unsupported fixed financial input format")
    summary = descriptor.get("summary")
    if (
        descriptor.get("format") != INPUT_FORMAT
        or not isinstance(summary, Mapping)
        or summary.get("projection_revision") != FINANCIAL_PROJECTION_REVISION
    ):
        raise SavedFinancialProjectionRevisionMismatch(
            "Saved J-Quants financial projection revision is absent or differs from this code; "
            "read the saved Generation or rebuild using its verified producer commit."
        )
    return fixed


def retain_financial_input(store: ObjectStore, selected: dict[str, Any]) -> dict[str, JsonValue]:
    return retain_full_day_input(store, selected, contract=_CONTRACT)


def read_financial_input(
    store: ObjectStore, descriptor: dict[str, JsonValue], *, days: Sequence[date]
) -> dict[str, Any]:
    return read_full_day_input(store, descriptor, days=days, contract=_CONTRACT)
