"""L1 store readers with provider-declared vintage selection.

Only providers whose spec declares publication-quality vintages are clamped to
the requested cutoff. Most bulk history stores the acquisition time as its
vintage, not the original publication time; clamping those providers would make
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

from .models import ObservationReader


def build_store_observation_reader(
    connection: sqlite3.Connection,
    *,
    series: Sequence[SeriesDefinition],
    vintage_cutoff: date | None = None,
) -> ObservationReader:
    """Bind one registry-aware observation reader to an open L1 connection.

    ``vintage_cutoff`` separates when facts were knowable from the observation
    range's end. Reading uses its as-of as the vintage cutoff only for providers
    that opt in; other providers retain the latest stored history, including later
    retrievals and revisions. No overall historical-knowledge guarantee follows.
    """

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
            vintage_on_or_before=vintage_cutoff,
        )

    return read


__all__ = [
    "build_store_observation_reader",
]
