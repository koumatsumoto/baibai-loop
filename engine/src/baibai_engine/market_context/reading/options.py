"""Read option indicators from retained native chains, not old scalar cache rows."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from baibai_engine.collection.economic_records import read_snapshots
from baibai_engine.market_context.indicators.definitions import SeriesDefinition
from baibai_engine.market_context.indicators.observations import ObservationRecord

from .observations import reading_observation
from .option_iv import OptionIvReadings, option_iv_readings, quotes_from_records

_READING_FIELDS = {
    "n225_iv_30d": "iv_30d",
    "n225_iv_skew": "iv_skew",
    "n225_iv_term": "iv_term",
}


class OptionSeriesReader:
    """Share small calculated readings, never decoded multi-year native chains.

    The owner is one read snapshot or acquisition pass. A changed native vintage
    replaces that day's cached reading; nothing is persisted as another source.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._readings: dict[date, tuple[datetime, OptionIvReadings]] = {}

    def read(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
    ) -> tuple[ObservationRecord, ...]:
        if series.unit != "percent":
            raise ValueError(f"option series {series.series_id} has inconsistent unit")
        field = _READING_FIELDS[series.reading_key]
        observations = []
        for chain in read_snapshots(self._connection, start, end, source_id=series.source_id):
            cached = self._readings.get(chain.observed_at)
            if cached is None or cached[0] != chain.fetched_at:
                readings = option_iv_readings(quotes_from_records(chain.records), chain.observed_at)
                self._readings[chain.observed_at] = (chain.fetched_at, readings)
            else:
                readings = cached[1]
            value = getattr(readings, field)
            if value is None:
                continue
            observations.append(
                reading_observation(
                    series,
                    observed_at=chain.observed_at,
                    value=value,
                    vintage_at=chain.fetched_at,
                )
            )
        return tuple(observations)
