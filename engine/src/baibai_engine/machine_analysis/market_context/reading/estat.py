"""Interpret retained e-Stat monthly cells; dashboard readings select final prints."""

from __future__ import annotations

import sqlite3
from datetime import date

from baibai_engine.data_collection.economic.records import NativeRecords, read_snapshots
from baibai_engine.machine_analysis.market_context.indicators.definitions import SeriesDefinition
from baibai_engine.machine_analysis.market_context.indicators.observations import ObservationRecord

from .observations import reading_observation

_NULL_MARKERS = frozenset({"", "-", "***", "X", "…", "..."})


def read_estat_series(
    connection: sqlite3.Connection, series: SeriesDefinition, *, start: date, end: date
) -> tuple[ObservationRecord, ...]:
    snapshots = tuple(read_snapshots(connection, start, end, source_id=series.source_id))
    values = estat_values(
        {snapshot.observed_at: snapshot.records for snapshot in snapshots},
        final_only=series.provider == "estat_dashboard",
    )
    return tuple(
        reading_observation(
            series,
            observed_at=snapshot.observed_at,
            value=values[snapshot.observed_at],
            vintage_at=snapshot.fetched_at,
        )
        for snapshot in snapshots
        if snapshot.observed_at in values
    )


def estat_values(records: NativeRecords, *, final_only: bool = False) -> dict[date, float]:
    """Project selected monthly cells and check units within the Reading window.

    Dashboard uses final prints only. A missing selected value stays missing,
    rather than reviving another duplicate's numeric value.
    Unit differences outside the Reading window do not invalidate that window.
    """
    observations: dict[date, float] = {}
    units: set[str] = set()
    for observed_at, rows in sorted(records.items()):
        selected_rows = [row for row in rows if not final_only or row.get("@isProvisional") == "0"]
        units.update(str(row.get("@unit")) for row in selected_rows)
        values = {_value(row.get("$")) for row in selected_rows}
        if not values or None in values:
            continue
        if len(values) != 1:
            raise ValueError("conflicting e-Stat monthly values")
        value = values.pop()
        if value is not None:
            observations[observed_at] = value
    if len(units) > 1:
        raise ValueError(f"e-Stat mixed units in one Reading: {', '.join(sorted(units))}")
    return observations


def _value(raw: object) -> float | None:
    if not isinstance(raw, str):
        raise ValueError(f"e-Stat value is not a string: {raw!r}")
    text = raw.strip()
    if text in _NULL_MARKERS:
        return None
    return float(text.replace(",", ""))
