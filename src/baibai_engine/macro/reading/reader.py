"""Point-in-time-aware L1 store readers for macro reading consumers.

Only providers whose spec declares publication-quality vintages are clamped to
the requested cutoff. Most bulk history stores the acquisition time as its
vintage, not the original publication time; clamping those providers would make
valid history disappear from snapshots whose as-of predates the backfill.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import date

from baibai_engine.macro.indicators import db as indicators_db
from baibai_engine.macro.indicators.db import ObservationRecord
from baibai_engine.macro.indicators.definitions import SeriesDefinition
from baibai_engine.macro.indicators.read_contracts import point_in_time_providers

type ObservationReader = Callable[[str, date, date], Sequence[ObservationRecord]]


def build_store_observation_reader(
    connection: sqlite3.Connection,
    *,
    series: Sequence[SeriesDefinition],
) -> ObservationReader:
    """Bind one registry-aware observation reader to an open L1 connection."""

    providers_by_series = {item.series_id: item.provider for item in series}
    point_in_time = point_in_time_providers()

    def read(series_id: str, start: date, end: date) -> tuple[ObservationRecord, ...]:
        provider = providers_by_series.get(series_id)
        if provider is None:
            raise KeyError(f"unknown indicator series: {series_id}")
        return indicators_db.observations_in_range(
            connection,
            series_id,
            start,
            end,
            point_in_time=provider in point_in_time,
        )

    return read


__all__ = ["ObservationReader", "build_store_observation_reader"]
