"""Freeze the native calendar basis used by acquisition, without a lake release."""

from __future__ import annotations

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
    VerifiedCalendarInput,
    calendar_day_session,
    read_calendar_input,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.storage.objects.r2 import ObjectStore
from baibai_jobs.integration.calendar_native import (
    prepare_input,
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


def _prepare_dependency(
    store: ObjectStore, *, directory: Path, observation_date: date
) -> VerifiedCalendarInput:
    fixed = CalendarL1(cast(CalendarObjectStore, store), directory).fixed()
    basis, _ = _context(fixed.value()["days"], observation_date)
    prepared = prepare_input(
        store,
        fixed,
        as_of=observation_date,
        evaluated_at=datetime.now(UTC),
        days=interval(basis, observation_date),
    )
    # The head can bound the search, but cannot establish the next dependency by itself.
    _context(prepared.scopes, observation_date)
    return prepared


def fix_calendar_dependency(
    store: ObjectStore, *, directory: Path, observation_date: date
) -> InputSnapshot:
    """Fix only the proved contiguous dates used by a new observation."""
    return _prepare_dependency(
        store, directory=directory, observation_date=observation_date
    ).snapshot


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
