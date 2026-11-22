"""Freeze the native calendar basis used by acquisition, without a lake release."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    CalendarL1,
    CalendarObjectStore,
    interval,
)
from baibai_engine.machine_analysis.generation.calendar_inputs import (
    calendar_day_session,
    read_calendar_input,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import ObjectStore
from baibai_jobs.integration.calendar_native import (
    fixed_input,
    materialize_saved_input,
    saved_interval,
)


class CalendarDependencyUnavailable(ValueError):
    """A continuous, resolved native calendar cannot establish the required basis."""


def _context(scopes: Mapping[str, Any], observation_date: date) -> tuple[date, bool]:
    day = observation_date
    while True:
        scope = scopes.get(day.isoformat(), {})
        business = calendar_day_session(scope)
        if business is None:
            raise CalendarDependencyUnavailable("continuous native calendar is unresolved")
        if business:
            return day, day == observation_date
        day -= timedelta(days=1)


def fix_calendar_dependency(
    store: ObjectStore, *, directory: Path, observation_date: date
) -> InputSnapshot:
    """Fix only the contiguous dates that prove this observation's master basis."""
    fixed = CalendarL1(cast(CalendarObjectStore, store), directory).fixed()
    basis, _ = _context(fixed.value()["days"], observation_date)
    return fixed_input(
        store,
        fixed,
        as_of=observation_date,
        evaluated_at=datetime.now(UTC),
        days=interval(basis, observation_date),
    )


def saved_calendar_context(
    store: ObjectStore, saved: InputSnapshot, *, observation_date: date
) -> tuple[date, bool]:
    """Resolve saved membership only; never re-read or fall back to the live head."""
    first, last = saved_interval(saved)
    if not first <= observation_date <= last:
        raise CalendarDependencyUnavailable("saved calendar does not cover the observation date")
    assert saved.descriptor is not None
    selected = read_calendar_input(store, saved.descriptor, days=interval(first, observation_date))
    return _context(selected["scopes"], observation_date)


def prepare_tradingview_calendar(
    store: ObjectStore, *, directory: Path, market: Path, observation_date: date
) -> bool:
    """Hydrate native calendar before master selection, retaining no duplicate raw pages."""
    report = json.loads((directory / "result.json").read_bytes())
    retained = report.pop("calendar_native_input", None)
    try:
        saved = (
            InputSnapshot.model_validate(retained)
            if retained is not None
            else fix_calendar_dependency(
                store, directory=directory / "calendar-native", observation_date=observation_date
            )
        )
        if (
            saved.descriptor is None
            or saved.descriptor.get("as_of") != observation_date.isoformat()
        ):
            raise CalendarDependencyUnavailable("fixed calendar observation date changed")
        saved_calendar_context(store, saved, observation_date=observation_date)
        materialize_saved_input(store, saved, market=market)
    except CalendarDependencyUnavailable:
        # This disposable input must not use an older release's holiday proof.
        with sqlite3.connect(market) as connection:
            connection.execute("DELETE FROM jquants_market_calendar")
            connection.execute("DELETE FROM source_coverage WHERE source='jquants_market_calendar'")
        write_json_atomic(directory / "result.json", report)
        return False
    report["calendar_native_input"] = saved.model_dump(mode="json")
    write_json_atomic(directory / "result.json", report)
    return True
