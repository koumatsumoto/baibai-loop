"""Selected L1 observations and transient L2 values from the same input snapshot.

Only providers whose spec declares publication-date vintages are clamped to
the requested cutoff. This does not establish exact historical API availability.
Most bulk history stores the acquisition time as its vintage, not the original
publication time; clamping those providers would make
valid history disappear from snapshots whose as-of predates the backfill.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date

from baibai_engine.market_context.indicators import db as indicators_db
from baibai_engine.market_context.indicators.definitions import SeriesDefinition
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.market_context.indicators.read_contracts import point_in_time_providers

from .derived import derive_series
from .formulas import FORMULAS
from .models import ObservationReader


def build_store_observation_reader(
    connection: sqlite3.Connection,
    *,
    series: Sequence[SeriesDefinition],
    vintage_cutoff: date | None = None,
) -> ObservationReader:
    """Bind one registry-aware observation reader to an open L1 connection.

    ``vintage_cutoff`` separates the source publication-date cutoff from the
    observation range's end. Reading uses its as-of as the cutoff only for providers
    that opt in; other providers retain the latest stored history, including later
    retrievals and revisions. No overall historical-knowledge guarantee follows.
    """

    definitions = {item.series_id: item for item in series}
    point_in_time = point_in_time_providers()

    def read(series_id: str, start: date, end: date) -> tuple[ObservationRecord, ...]:
        definition = definitions.get(series_id)
        if definition is None:
            raise KeyError(f"unknown indicator series: {series_id}")
        if definition.provider == "derived":
            formula = FORMULAS[definition.provider_series_id]
            input_start = start.replace(day=1) if formula.alignment == "monthly" else start
            inputs = {key: read(key, input_start, end) for key in formula.inputs}
            return derive_series(definition, inputs, start=start, end=end)
        return indicators_db.observations_in_range(
            connection,
            series_id,
            start,
            end,
            point_in_time=definition.provider in point_in_time,
            vintage_on_or_before=vintage_cutoff,
        )

    return read


__all__ = [
    "build_store_observation_reader",
]
