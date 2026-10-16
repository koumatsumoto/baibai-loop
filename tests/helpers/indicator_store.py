"""Builders for the indicator store: series, observations, readers, and seeds."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from baibai_engine.market_context.indicators.db import initialize_database, insert_observations
from baibai_engine.market_context.indicators.definitions import SeriesDefinition
from baibai_engine.market_context.indicators.observations import ObservationRecord

SOURCE_URL = "https://example.com/data.csv"

def series_definition(
    series_id: str = "test.series",
    *,
    provider: str = "fred_csv",
    provider_series_id: str = "TEST",
    name: str = "Test Series",
    category: str = "rates",
    geography: str = "world",
    frequency: str = "daily",
    unit: str = "percent",
    source_id: str = "test-source",
    source_url: str = SOURCE_URL,
    plausible_min: float | None = None,
    plausible_max: float | None = None,
) -> SeriesDefinition:
    """A registry entry with every required field filled in.

    Which field a test varies is the test's subject; the rest only has to be valid,
    and having one definition of "valid" means a registry field lands in one place.
    """

    return SeriesDefinition(
        series_id=series_id,
        name=name,
        category=category,
        geography=geography,
        frequency=frequency,
        unit=unit,
        provider=provider,
        provider_series_id=provider_series_id,
        source_id=source_id,
        source_url=source_url,
        plausible_min=plausible_min,
        plausible_max=plausible_max,
    )


def observation(
    series: str | SeriesDefinition,
    observed_at: date,
    value: float,
    vintage_at: datetime | None = None,
    *,
    unit: str | None = None,
    source_url: str | None = None,
    period_days: int | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    fetch_status: str = "ok",
) -> ObservationRecord:
    """One stored observation.

    Passing a `SeriesDefinition` takes its unit and source URL, because a record whose
    unit disagrees with its series is one the store's own triggers refuse — a fixture
    that builds it is testing a shape production never writes. ``period_days`` spells
    the flow case: a window of that many days ending at ``observed_at``.
    """

    if period_days is not None:
        if period_start is not None or period_end is not None:
            raise ValueError("give period_days or explicit bounds, not both")
        period_start = observed_at - timedelta(days=period_days)
        period_end = observed_at
    return ObservationRecord(
        series_id=series if isinstance(series, str) else series.series_id,
        observed_at=observed_at,
        value=value,
        unit=unit if unit is not None else ("percent" if isinstance(series, str) else series.unit),
        source_url=source_url
        if source_url is not None
        else (SOURCE_URL if isinstance(series, str) else series.source_url),
        period_start=period_start,
        period_end=period_end,
        vintage_at=vintage_at,
        fetch_status=fetch_status,
    )


def observations(
    series: str | SeriesDefinition,
    points: Iterable[tuple[date, float]],
    *,
    unit: str | None = None,
    source_url: str | None = None,
    vintage_at: Callable[[date], datetime] | None = None,
) -> tuple[ObservationRecord, ...]:
    """A series of observations, optionally vintaged from each observation date."""

    return tuple(
        observation(
            series,
            observed_at,
            value,
            unit=unit,
            source_url=source_url,
            vintage_at=None if vintage_at is None else vintage_at(observed_at),
        )
        for observed_at, value in points
    )


def store_reader(
    records: Sequence[ObservationRecord],
) -> Callable[[str, date, date], tuple[ObservationRecord, ...]]:
    """The read callable the macro readers are handed, backed by a list."""

    def read(series_id: str, start: date, end: date) -> tuple[ObservationRecord, ...]:
        return tuple(
            record
            for record in records
            if record.series_id == series_id and start <= record.observed_at <= end
        )

    return read


def seed_store(path: Path, *records: ObservationRecord) -> None:
    """Create an indicator store at `path` holding exactly `records`."""

    connection = initialize_database(path)
    try:
        if records:
            insert_observations(connection, list(records))
    finally:
        connection.close()


__all__ = [
    "SOURCE_URL",
    "observation",
    "observations",
    "seed_store",
    "series_definition",
    "store_reader",
]
