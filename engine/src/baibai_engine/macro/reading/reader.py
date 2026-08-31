"""Point-in-time-aware L1 store readers for macro reading consumers.

Only providers whose spec declares publication-quality vintages are clamped to
the requested cutoff. Most bulk history stores the acquisition time as its
vintage, not the original publication time; clamping those providers would make
valid history disappear from snapshots whose as-of predates the backfill.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import date, datetime

from baibai_engine.macro.indicators import db as indicators_db
from baibai_engine.macro.indicators.db import ObservationRecord
from baibai_engine.macro.indicators.definitions import SeriesDefinition
from baibai_engine.macro.indicators.read_contracts import point_in_time_providers

type ObservationReader = Callable[[str, date, date], Sequence[ObservationRecord]]


def build_store_observation_reader(
    connection: sqlite3.Connection,
    *,
    series: Sequence[SeriesDefinition],
    vintage_cutoff: date | None = None,
) -> ObservationReader:
    """Bind one registry-aware observation reader to an open L1 connection.

    ``vintage_cutoff`` separates when facts were knowable from the observation
    range's end. Reading snapshots leave it unset because their as-of is both;
    scorecard settlement fixes it to the evaluation date while each condition
    retains its own observation deadline.
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


def build_multi_asof_store_observation_reader(
    connection: sqlite3.Connection,
    *,
    series: Sequence[SeriesDefinition],
    max_asof: date,
) -> ObservationReader:
    """Read each series once, then replay multiple as-of cutoffs in memory.

    A daily brief compares the current, previous-data-day, and latest-context
    readings. Running the ordinary reader three times repeats the same SQLite range
    scan three times per series. This reader loads the raw vintages once on first use
    and applies the same latest-vintage/retraction and point-in-time rules for each
    requested ``end`` date. It is a request-local cache, not stored state.
    """

    providers_by_series = {item.series_id: item.provider for item in series}
    point_in_time = point_in_time_providers()
    cached: dict[str, tuple[ObservationRecord, ...]] = {}

    def load(series_id: str) -> tuple[ObservationRecord, ...]:
        if series_id not in providers_by_series:
            raise KeyError(f"unknown indicator series: {series_id}")
        if series_id not in cached:
            rows = connection.execute(
                "SELECT series_id, observed_at, period_start, period_end, value, unit, "
                "vintage_at, fetch_status, source_url FROM observations "
                "WHERE series_id = ? AND observed_at <= ? "
                "AND fetch_status IN ('ok', 'retracted') "
                "ORDER BY observed_at, vintage_at",
                (series_id, max_asof.isoformat()),
            ).fetchall()
            cached[series_id] = tuple(_observation_from_row(row) for row in rows)
        return cached[series_id]

    def read(series_id: str, start: date, end: date) -> tuple[ObservationRecord, ...]:
        provider = providers_by_series.get(series_id)
        if provider is None:
            raise KeyError(f"unknown indicator series: {series_id}")
        candidates: dict[date, ObservationRecord] = {}
        for observation in load(series_id):
            if observation.observed_at < start or observation.observed_at > end:
                continue
            if (
                provider in point_in_time
                and observation.vintage_at is not None
                and observation.vintage_at.date() > end
            ):
                continue
            candidates[observation.observed_at] = observation
        return tuple(
            observation
            for _, observation in sorted(candidates.items())
            if observation.fetch_status == "ok"
        )

    return read


def _observation_from_row(row: sqlite3.Row) -> ObservationRecord:
    period_start = row["period_start"]
    period_end = row["period_end"]
    vintage_at = row["vintage_at"]
    return ObservationRecord(
        series_id=str(row["series_id"]),
        observed_at=date.fromisoformat(str(row["observed_at"])),
        period_start=date.fromisoformat(str(period_start)) if period_start else None,
        period_end=date.fromisoformat(str(period_end)) if period_end else None,
        value=float(row["value"]),
        unit=str(row["unit"]),
        vintage_at=datetime.fromisoformat(str(vintage_at)) if vintage_at else None,
        fetch_status=str(row["fetch_status"]),
        source_url=str(row["source_url"]),
    )


__all__ = [
    "ObservationReader",
    "build_multi_asof_store_observation_reader",
    "build_store_observation_reader",
]
