"""Shared immutable price input selections; source bodies remain in L1."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from pydantic import JsonValue

from baibai_engine.data_collection.providers.jquants.calendar_native import read_calendar_attempts
from baibai_engine.data_collection.providers.jquants.contracts import complete_request
from baibai_engine.machine_analysis.generation.calendar_inputs import (
    INPUT_FORMAT as CALENDAR_INPUT_FORMAT,
)
from baibai_engine.machine_analysis.generation.calendar_inputs import (
    calendar_day_closed,
    read_calendar_input,
    retain_calendar_input,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.full_day_inputs import (
    FullDayInputContract,
    read_full_day_input,
    retain_full_day_input,
)
from baibai_engine.storage.objects.r2 import ObjectStore

INPUT_FORMAT = "jquants-price-input-v2"
INPUT_PREFIX = "fixed-inputs/jquants-price/v1/"
_CONTRACT = FullDayInputContract(
    INPUT_PREFIX,
    INPUT_FORMAT,
    "jquants-price-selection-v1",
    "jquants-price-month-v1",
    allow_legacy_inline=False,
)
_LEGACY_CONTRACT = replace(_CONTRACT, input_format="jquants-price-input-v1")


def with_calendar_closures(
    store: ObjectStore,
    scopes: dict[str, Any],
    calendar: InputSnapshot | None,
    *,
    evaluated_at: datetime,
) -> dict[str, Any]:
    """Bind only unattempted price dates to a fixed, safely closed calendar day.

    This does not create an empty price receipt or change acquisition planning.
    The dependency is inside the immutable price month, hence its lineage root.
    """
    if calendar is None:
        return scopes
    calendar = InputSnapshot.model_validate_json(calendar.model_dump_json())
    descriptor = calendar.descriptor
    if descriptor is None or descriptor.get("format") != CALENDAR_INPUT_FORMAT:
        raise ValueError("price closure requires a fixed native calendar input")
    summary, digest = descriptor.get("summary"), descriptor.get("selection_sha256")
    if not isinstance(summary, dict) or not isinstance(digest, str):
        raise ValueError("price closure requires its calendar interval and digest")
    clock = summary.get("evaluated_at")
    if not isinstance(clock, str) or datetime.fromisoformat(clock) > evaluated_at:
        raise ValueError("price closure calendar is later than its evaluation clock")
    first, last = summary.get("selection_start"), summary.get("selection_end")
    if not isinstance(first, str) or not isinstance(last, str):
        raise ValueError("price closure requires its calendar interval")
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    days = [
        date.fromisoformat(day)
        for day, scope in scopes.items()
        if not scope and start <= date.fromisoformat(day) <= end
    ]
    if not days:
        return scopes
    selected = read_calendar_input(store, descriptor, days=days)
    months: dict[str, dict[str, Any]] = defaultdict(dict)
    for day, scope in selected["scopes"].items():
        if calendar_day_closed(scope):
            months[day[:7]][day] = scope
    closed = {}
    for month_scopes in months.values():
        # Reuse the calendar's selection format, bounded to this month's proof.
        # An unrelated month or a later evaluation must not recopy price history.
        proof = retain_calendar_input(store, {**descriptor, **selected, "scopes": month_scopes})
        closed.update({day: {"closed_calendar": proof["selection_sha256"]} for day in month_scopes})
    return {**scopes, **closed}


def read_closed_price_days(store: ObjectStore, scopes: dict[str, Any]) -> set[date]:
    """Verify retained holiday evidence without reading a mutable head or L2."""
    by_calendar: dict[str, list[date]] = defaultdict(list)
    for day, scope in scopes.items():
        if "closed_calendar" not in scope:
            continue
        digest = scope["closed_calendar"]
        if set(scope) != {"closed_calendar"} or not isinstance(digest, str):
            raise ValueError("calendar closure cannot replace a price attempt")
        by_calendar[digest].append(date.fromisoformat(day))
    closed = set()
    for digest, days in by_calendar.items():
        selected = read_calendar_input(
            store,
            {"format": CALENDAR_INPUT_FORMAT, "selection_sha256": digest},
            days=days,
        )
        if not all(calendar_day_closed(scope) for scope in selected["scopes"].values()):
            raise ValueError("fixed price closure is not proved by its calendar")
        adopted = {scope["adopted"] for scope in selected["scopes"].values()}
        for attempt in read_calendar_attempts(
            store, receipts=selected["receipts"], identities=adopted
        ):
            complete_request(attempt.pages)
            if any(
                not attempt.start <= date.fromisoformat(day) <= attempt.end
                for day, scope in selected["scopes"].items()
                if scope["adopted"] == attempt.request_id
            ):
                raise ValueError("calendar closure request differs from its fixed dates")
        closed.update(days)
    return closed


def retain_price_input(store: ObjectStore, selected: dict[str, Any]) -> dict[str, JsonValue]:
    return retain_full_day_input(store, selected, contract=_CONTRACT)


def read_price_input(
    store: ObjectStore, descriptor: dict[str, JsonValue], *, days: Sequence[date]
) -> dict[str, Any]:
    contract = (
        _LEGACY_CONTRACT if descriptor.get("format") == _LEGACY_CONTRACT.input_format else _CONTRACT
    )
    return read_full_day_input(store, descriptor, days=days, contract=contract)
