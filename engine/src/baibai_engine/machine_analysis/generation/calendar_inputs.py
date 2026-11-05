"""Immutable monthly calendar day selections, referencing intact native range packs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.full_day_inputs import (
    FullDayInputContract,
    read_full_day_input,
    retain_full_day_input,
)
from baibai_engine.storage.objects.r2 import ObjectStore

INPUT_FORMAT = "jquants-market-calendar-input-v1"
INPUT_PREFIX = "fixed-inputs/jquants-market-calendar/v1/"
_CONTRACT = FullDayInputContract(
    INPUT_PREFIX,
    INPUT_FORMAT,
    "jquants-market-calendar-selection-v1",
    "jquants-market-calendar-month-v1",
    receipt_fields=("adopted", "failure", "held", "confirmation_id"),
)


def calendar_day_usable(scope: Mapping[str, Any]) -> bool:
    """Only resolved, recognized native day evidence proves a cash-equity session."""
    return bool(
        scope.get("adopted")
        and scope.get("valid")
        and scope.get("division") in {"0", "1", "2", "3"}
        and not scope.get("failure")
        and not scope.get("held")
    )


def calendar_day_closed(scope: Mapping[str, Any]) -> bool:
    return calendar_day_usable(scope) and scope["division"] in {"0", "3"}


def retain_calendar_input(store: ObjectStore, selected: dict[str, Any]) -> dict[str, JsonValue]:
    return retain_full_day_input(store, selected, contract=_CONTRACT)


def read_calendar_input(
    store: ObjectStore, descriptor: dict[str, JsonValue], *, days: Sequence[date]
) -> dict[str, Any]:
    return read_full_day_input(store, descriptor, days=days, contract=_CONTRACT)
