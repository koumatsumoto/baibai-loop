"""Immutable monthly calendar day selections, referencing intact native range packs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.full_day_inputs import (
    FullDayInputContract,
    read_full_day_input,
    retain_full_day_input,
)
from baibai_engine.primitives.jquants_calendar import cash_equity_open
from baibai_engine.storage.objects.r2 import ObjectStore

CALENDAR_PROJECTION_REVISION = "jquants-calendar-projection-v2-native-day-proof"
INPUT_FORMAT = "jquants-market-calendar-input-v1"
INPUT_PREFIX = "fixed-inputs/jquants-market-calendar/v1/"
_CONTRACT = FullDayInputContract(
    INPUT_PREFIX,
    INPUT_FORMAT,
    "jquants-market-calendar-selection-v1",
    "jquants-market-calendar-month-v1",
    receipt_fields=("adopted", "failure", "held", "confirmation_id"),
)


@dataclass(frozen=True, slots=True)
class VerifiedCalendarInput:
    """One preparation's checked day claims, paired with its immutable input identity."""

    snapshot: InputSnapshot
    scopes: Mapping[str, Mapping[str, Any]]
    receipts: Mapping[str, Mapping[str, Any]]
    full_head: bool


def calendar_day_session(scope: Mapping[str, Any]) -> bool | None:
    """Only resolved, recognized native day evidence proves a cash-equity session."""
    if (
        not scope.get("adopted")
        or not scope.get("valid")
        or scope.get("failure")
        or scope.get("held")
    ):
        return None
    return cash_equity_open(scope.get("division"))


def calendar_day_usable(scope: Mapping[str, Any]) -> bool:
    return calendar_day_session(scope) is not None


def calendar_day_closed(scope: Mapping[str, Any]) -> bool:
    return calendar_day_session(scope) is False


def retain_calendar_input(
    store: ObjectStore, selected: dict[str, Any], *, immediate_full_replay: bool = False
) -> dict[str, JsonValue]:
    return retain_full_day_input(
        store, selected, contract=_CONTRACT, immediate_full_replay=immediate_full_replay
    )


def read_calendar_input(
    store: ObjectStore, descriptor: dict[str, JsonValue], *, days: Sequence[date]
) -> dict[str, Any]:
    return read_full_day_input(store, descriptor, days=days, contract=_CONTRACT)
