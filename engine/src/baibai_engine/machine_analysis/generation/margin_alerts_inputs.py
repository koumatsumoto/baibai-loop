"""Immutable monthly margin-alert selections, without daily whole-history copies."""

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

INPUT_FORMAT = "jquants-margin-alerts-input-v1"
INPUT_PREFIX = "fixed-inputs/jquants-margin-alerts/v1/"
_CONTRACT = FullDayInputContract(
    INPUT_PREFIX,
    INPUT_FORMAT,
    "jquants-margin-alerts-selection-v1",
    "jquants-margin-alerts-month-v1",
)


def retain_margin_alerts_input(
    store: ObjectStore, selected: dict[str, Any]
) -> dict[str, JsonValue]:
    return retain_full_day_input(store, selected, contract=_CONTRACT)


def read_margin_alerts_input(
    store: ObjectStore, descriptor: dict[str, JsonValue], *, days: Sequence[date]
) -> dict[str, Any]:
    return read_full_day_input(store, descriptor, days=days, contract=_CONTRACT)
