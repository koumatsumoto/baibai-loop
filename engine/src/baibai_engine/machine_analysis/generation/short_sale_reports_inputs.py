"""Immutable monthly short-sale-report selections, without daily whole-history copies."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.full_day_inputs import (
    FullDayInputContract,
    full_day_input_interval,
    read_full_day_input,
    retain_full_day_input,
)
from baibai_engine.storage.objects.r2 import ObjectStore

INPUT_FORMAT = "jquants-short-sale-reports-input-v1"
INPUT_PREFIX = "fixed-inputs/jquants-short-sale-reports/v1/"
_CONTRACT = FullDayInputContract(
    INPUT_PREFIX,
    INPUT_FORMAT,
    "jquants-short-sale-reports-selection-v1",
    "jquants-short-sale-reports-month-v1",
)


def retain_short_sale_reports_input(
    store: ObjectStore, selected: dict[str, Any]
) -> dict[str, JsonValue]:
    return retain_full_day_input(store, selected, contract=_CONTRACT)


def read_short_sale_reports_input(
    store: ObjectStore, descriptor: dict[str, JsonValue], *, days: Sequence[date]
) -> dict[str, Any]:
    return read_full_day_input(store, descriptor, days=days, contract=_CONTRACT)


def short_sale_reports_input_interval(
    store: ObjectStore, descriptor: dict[str, JsonValue]
) -> tuple[date, date]:
    return full_day_input_interval(store, descriptor, contract=_CONTRACT)
