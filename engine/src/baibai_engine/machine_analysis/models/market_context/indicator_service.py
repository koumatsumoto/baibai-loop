"""Read reproducible L2 indicator values; acquisition belongs to Collection."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.economic.store import DEFAULT_DB_PATH, open_read_only_connection

from baibai_engine.machine_analysis.models.market_context.indicators.definitions import SeriesDefinition, load_definitions
from baibai_engine.machine_analysis.models.market_context.indicators.observations import ObservationRecord
from baibai_engine.machine_analysis.models.market_context.reading.reader import build_store_observation_reader

_LATEST_READ_DAYS = {"daily": 14, "weekly": 60, "monthly": 370}


@dataclass(frozen=True)
class QueryResult:
    series: SeriesDefinition
    observations: tuple[ObservationRecord, ...]


def list_series(*, category: str | None = None) -> tuple[SeriesDefinition, ...]:
    return tuple(
        sorted(
            (
                series
                for series in load_definitions().series
                if category is None or series.category == category
            ),
            key=lambda series: (series.priority, series.series_id),
        )
    )


def search(query: str) -> tuple[SeriesDefinition, ...]:
    return tuple(
        sorted(
            load_definitions().search(query), key=lambda series: (series.priority, series.series_id)
        )
    )


class IndicatorsService:
    """Query a saved input snapshot without writing, fetching, or seeding metadata."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    def get_range(self, series_id: str, *, start: date, end: date) -> QueryResult:
        if end < start:
            raise ValueError("end must be on or after start")
        definitions = load_definitions()
        registered = definitions.by_id()
        if series_id not in registered:
            raise KeyError(f"unknown indicator series: {series_id}")
        with closing(open_read_only_connection(self.db_path)) as connection:
            connection.execute("BEGIN")
            reader = build_store_observation_reader(connection, series=definitions.series)
            observations = tuple(reader(series_id, start, end))
        return QueryResult(registered[series_id], observations)

    def get_latest(self, series_id: str) -> QueryResult:
        end = _today_jst()
        definitions = load_definitions()
        registered = definitions.by_id()
        if series_id not in registered:
            raise KeyError(f"unknown indicator series: {series_id}")
        series = registered[series_id]
        floor = date(1970, 1, 1) if series.provider == "derived" else date.min
        days = _LATEST_READ_DAYS.get(series.frequency, 370)
        with closing(open_read_only_connection(self.db_path)) as connection:
            connection.execute("BEGIN")
            reader = build_store_observation_reader(
                connection, series=definitions.series, vintage_cutoff=end
            )
            while True:
                start = (
                    floor
                    if series.provider == "derived"
                    else end - timedelta(days=min(days, (end - floor).days))
                )
                observations = tuple(reader(series_id, start, end))
                if observations or start == floor:
                    return QueryResult(series, observations[-1:])
                # Search older saved observations without recomputing recent
                # option chains or treating an old date as today's observation.
                end = start - timedelta(days=1)
                days *= 2


def _today_jst() -> date:
    return datetime.now(ZoneInfo("Asia/Tokyo")).date()
