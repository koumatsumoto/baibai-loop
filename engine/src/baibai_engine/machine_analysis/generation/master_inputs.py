"""Immutable master snapshot selections; dates are sparse, not daily obligations."""

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

INPUT_FORMAT = "jquants-master-input-v1"
INPUT_PREFIX = "fixed-inputs/jquants-master/v1/"
_CONTRACT = FullDayInputContract(
    INPUT_PREFIX,
    INPUT_FORMAT,
    "jquants-master-selection-v1",
    "jquants-master-month-v1",
)


def retain_master_input(store: ObjectStore, selected: dict[str, Any]) -> dict[str, JsonValue]:
    return retain_full_day_input(store, selected, contract=_CONTRACT)


def read_master_input(
    store: ObjectStore, descriptor: dict[str, JsonValue], *, days: Sequence[date]
) -> dict[str, Any]:
    selected = read_full_day_input(store, descriptor, days=days, contract=_CONTRACT)
    # The interval bounds immutable metadata, not a promise to request every day.
    selected["scopes"] = {day: scope for day, scope in selected["scopes"].items() if scope}
    return selected
