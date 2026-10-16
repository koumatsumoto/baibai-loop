"""Application-owned acquisition of reusable daily option-chain inputs."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

from baibai_engine.foundation.redaction import redact_credentials

from .indicators import db
from .indicators.definitions import SeriesDefinition
from .indicators.observations import ObservationRecord
from .indicators.providers.base import (
    FetchContext,
    IndicatorsProviderError,
    ProviderFetchStoppedError,
)
from .indicators.providers.jquants_options import OptionChainSource
from .indicators.source_snapshots import store_snapshot, stored_days
from .reading.options import OptionSeriesReader

_RECENT_REFRESH_DAYS = 14


class OptionAcquisition:
    """Share acquisition across the three readings, with durable progress per day.

    A history rebuild reuses completed old days and refreshes only its recent tail.
    An explicit bounded refresh can restate any selected older interval. Neither
    operation stores computed readings as if they were provider observations.
    """

    def __init__(self, connection: sqlite3.Connection, context: FetchContext) -> None:
        self._connection = connection
        self._context = context
        self._reader = OptionSeriesReader(connection)
        self._source: OptionChainSource | None = None
        self._completed: set[date] = set()
        self._failures: dict[date, str] = {}

    def close(self) -> None:
        if self._source is not None:
            self._source.close()

    def query(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        refresh: bool,
        require_observations: bool = False,
    ) -> tuple[tuple[ObservationRecord, ...], bool]:
        started_at = datetime.now(UTC)
        fetched = False
        try:
            known = stored_days(self._connection, start, end, source_key="jquants_options:n225")
            for offset in range((end - start).days + 1):
                day = start + timedelta(days=offset)
                if day.weekday() >= 5:
                    continue
                if day in self._failures:
                    raise ProviderFetchStoppedError(self._failures[day])
                restate = refresh and (
                    self._context.purpose != "rebuild"
                    or day >= end - timedelta(days=_RECENT_REFRESH_DAYS)
                )
                if day in self._completed or (day in known and not restate):
                    continue
                if self._source is None:
                    self._source = OptionChainSource(self._context)
                try:
                    records = self._source.fetch(day)
                except IndicatorsProviderError as error:
                    self._failures[day] = str(error)
                    raise
                # A later transport or calculation failure does not roll back days
                # already received. No transaction spans the external request.
                with self._connection:
                    store_snapshot(
                        self._connection,
                        source_key="jquants_options:n225",
                        observed_at=day,
                        fetched_at=datetime.now(UTC),
                        records=records,
                    )
                self._completed.add(day)
                fetched = True
            observations = self._reader.read(series, start=start, end=end)
            if require_observations and not observations:
                raise IndicatorsProviderError(
                    f"all-history refresh returned no observations for {series.series_id}"
                )
        except sqlite3.Error:
            raise
        except Exception as error:
            with self._connection:
                db.record_provider_run(
                    self._connection,
                    provider=series.provider,
                    series_id=series.series_id,
                    start=start,
                    end=end,
                    started_at=started_at,
                    status="failed",
                    record_count=0,
                    error_message=redact_credentials(str(error)),
                )
            raise
        if fetched or refresh:
            with self._connection:
                db.record_provider_run(
                    self._connection,
                    provider=series.provider,
                    series_id=series.series_id,
                    start=start,
                    end=end,
                    started_at=started_at,
                    status="ok",
                    record_count=len(observations),
                )
        return observations, not fetched
