"""Choose final dashboard values without discarding the retained publication states."""

from __future__ import annotations

import sqlite3
from datetime import date

from baibai_engine.market_context.indicators.definitions import SeriesDefinition
from baibai_engine.market_context.indicators.observations import NativeRecords, ObservationRecord
from baibai_engine.market_context.indicators.read_contracts import native_source_key
from baibai_engine.market_context.indicators.source_snapshots import read_snapshots

from .observations import reading_observation

_NULL_MARKERS = frozenset({"", "-", "***", "X", "…", "..."})


def read_dashboard_series(
    connection: sqlite3.Connection, series: SeriesDefinition, *, start: date, end: date
) -> tuple[ObservationRecord, ...]:
    snapshots = tuple(read_snapshots(connection, start, end, source_key=native_source_key(series)))
    values = dashboard_values({snapshot.observed_at: snapshot.records for snapshot in snapshots})
    return tuple(
        reading_observation(
            series, observed_at=snapshot.observed_at, value=values[snapshot.observed_at],
            vintage_at=snapshot.fetched_at,
        )
        for snapshot in snapshots if snapshot.observed_at in values
    )


def dashboard_values(records: NativeRecords) -> dict[date, float]:
    """Project the requested window, checking units only for its final prints.

    A preliminary value never replaces a final value. A missing final value is
    still missing, rather than reviving another duplicate's numeric value.
    Unit differences outside the Reading window do not invalidate that window.
    """
    observations: dict[date, float] = {}
    units: set[str] = set()
    for observed_at, rows in sorted(records.items()):
        final_rows = [row for row in rows if row.get("@isProvisional") == "0"]
        units.update(str(row.get("@unit")) for row in final_rows)
        values = {_value(row.get("$")) for row in final_rows}
        if not values or None in values:
            continue
        if len(values) != 1:
            raise ValueError("conflicting final e-Stat dashboard values")
        value = values.pop()
        if value is not None:
            observations[observed_at] = value
    if len(units) > 1:
        raise ValueError(f"e-Stat dashboard mixed units in one Reading: {', '.join(sorted(units))}")
    return observations


def _value(raw: object) -> float | None:
    if not isinstance(raw, str):
        raise ValueError(f"e-Stat dashboard value is not a string: {raw!r}")
    text = raw.strip()
    if text in _NULL_MARKERS:
        return None
    return float(text.replace(",", ""))
