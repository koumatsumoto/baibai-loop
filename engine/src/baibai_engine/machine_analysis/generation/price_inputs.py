"""Shared immutable price input selections; source bodies remain in L1."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.full_day_inputs import (
    FullDayInputContract,
    read_full_day_input,
    retain_full_day_input,
)
from baibai_engine.storage.objects.r2 import ObjectStore

INPUT_FORMAT = "jquants-price-input-v1"
INPUT_PREFIX = "fixed-inputs/jquants-price/v1/"
_CONTRACT = FullDayInputContract(
    INPUT_PREFIX,
    INPUT_FORMAT,
    "jquants-price-selection-v1",
    "jquants-price-month-v1",
    allow_legacy_inline=False,
)


def retain_price_input(store: ObjectStore, selected: dict[str, Any]) -> dict[str, JsonValue]:
    return retain_full_day_input(store, selected, contract=_CONTRACT)


def read_price_input(
    store: ObjectStore, descriptor: dict[str, JsonValue], *, days: Sequence[date]
) -> dict[str, Any]:
    return read_full_day_input(store, descriptor, days=days, contract=_CONTRACT)
