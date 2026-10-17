"""Builders for indicator series, temporary L2 observations, and source fixtures."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta

from baibai_engine.collection.economic_records import store_snapshot
from baibai_engine.market_context.indicators.definitions import SeriesDefinition, load_definitions
from baibai_engine.market_context.indicators.observations import ObservationRecord

SOURCE_URL = "https://example.com/data.csv"


def series_definition(
    series_id: str = "test.series",
    *,
    provider: str = "mof_jgb",
    reading_key: str = "TEST",
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
        reading_key=reading_key,
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
    """One temporary L2 calculation value used by a reader fixture.

    Passing a `SeriesDefinition` takes its unit and source URL. This record is not
    retained source input. ``period_days`` is the start-to-end date offset, not an
    inclusive day count (4 means Monday to Friday).
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


def store_h15_observations(
    connection: sqlite3.Connection, records: Sequence[ObservationRecord]
) -> None:
    """Seed synthetic complete native H.15 rows from explicit yield fixture values.

    The fixture includes every registered maturity, with empty cells for yields the
    test does not supply. It is not an original CSV response or a production adapter.
    """
    registry = load_definitions().by_id()
    definitions = {item.series_id: item for item in registry.values() if item.provider == "frb_h15"}
    grouped: dict[tuple[date, datetime], dict[str, str]] = {}
    for record in records:
        definition = definitions[record.series_id]
        if record.fetch_status != "ok":
            raise ValueError("H.15 fixture expects a successful source value")
        fetched_at = record.vintage_at or datetime.combine(
            record.observed_at, datetime.min.time(), tzinfo=UTC
        )
        key = (record.observed_at, fetched_at)
        row = grouped.setdefault(
            key,
            {"Time Period": record.observed_at.isoformat()}
            | {item.reading_key: "" for item in definitions.values()},
        )
        row[definition.reading_key] = str(record.value)
    for (observed_at, fetched_at), row in grouped.items():
        store_snapshot(
            connection,
            source_id=definitions[records[0].series_id].source_id,
            observed_at=observed_at,
            fetched_at=fetched_at,
            records=[{"row": row, "preamble": []}],
        )


def store_mof_observations(
    connection: sqlite3.Connection, records: Sequence[ObservationRecord]
) -> None:
    """Seed synthetic MOF rows; not a conversion of historical production data."""
    registry = load_definitions().by_id()
    definitions = {key: value for key, value in registry.items() if value.provider == "mof_jgb"}
    grouped: dict[tuple[date, datetime], dict[str, str]] = {}
    for record in records:
        definition = definitions[record.series_id]
        if record.fetch_status != "ok":
            raise ValueError("MOF fixture expects a successful source value")
        fetched_at = record.vintage_at or datetime.combine(
            record.observed_at, datetime.min.time(), tzinfo=UTC
        )
        row = grouped.setdefault(
            (record.observed_at, fetched_at),
            {
                "基準日": f"R{record.observed_at.year - 2018}.{record.observed_at.month}.{record.observed_at.day}",
                **{item.reading_key: "" for item in definitions.values()},
            },
        )
        row[definition.reading_key] = str(record.value)
    for (observed_at, fetched_at), row in grouped.items():
        definition = definitions[records[0].series_id]
        store_snapshot(
            connection,
            source_id=definition.source_id,
            observed_at=observed_at,
            fetched_at=fetched_at,
            records=[{"row": row, "preamble": [], "source_url": definition.source_url}],
        )


__all__ = [
    "SOURCE_URL",
    "observation",
    "observations",
    "series_definition",
    "store_h15_observations",
    "store_mof_observations",
    "store_reader",
]
