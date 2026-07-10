from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import db
from .db import ObservationRecord
from .definitions import SeriesDefinition
from .providers import FetchContext, IndicatorsProviderError, fetch_observations

DEFAULT_LATEST_LOOKBACK_DAYS = 370
LATEST_FETCH_LOOKBACK_DAYS = {
    "daily": 14,
    "weekly": 60,
    "monthly": DEFAULT_LATEST_LOOKBACK_DAYS,
}
LATEST_CACHE_MAX_AGE_DAYS = {
    "daily": 1,
    "weekly": 14,
    "monthly": 70,
}
PROVIDER_FETCH_ATTEMPTS = 2
PROVIDER_FETCH_RETRY_BACKOFF_SECONDS = 1.0


@dataclass(frozen=True)
class QueryResult:
    series: SeriesDefinition
    observations: tuple[ObservationRecord, ...]
    cache_hit: bool


class IndicatorsService:
    def __init__(self, db_path: Path = db.DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    def list_series(self, *, category: str | None = None) -> tuple[SeriesDefinition, ...]:
        conn = db.open_connection(self.db_path)
        try:
            return db.list_series(conn, category=category)
        finally:
            conn.close()

    def search(self, query: str) -> tuple[SeriesDefinition, ...]:
        conn = db.open_connection(self.db_path)
        try:
            return db.search_series(conn, query)
        finally:
            conn.close()

    def get_range(
        self,
        series_id: str,
        *,
        start: date,
        end: date,
        refresh: bool = False,
    ) -> QueryResult:
        if end < start:
            raise ValueError("--end must be on or after --start")
        conn = db.open_connection(self.db_path)
        try:
            series = db.get_series(conn, series_id)
            cache_hit = (not refresh) and db.has_ok_coverage(conn, series_id, start, end)
            if not cache_hit:
                observations = self._fetch_and_store(conn, series, start=start, end=end)
            else:
                observations = list(db.observations_in_range(conn, series_id, start, end))
            # Providers return observations in source order (ECB FX is newest-first);
            # normalize to ascending observed_at so callers get a stable chronology
            # regardless of cache-hit vs provider-fetch path.
            ordered = sorted(observations, key=lambda item: item.observed_at)
            return QueryResult(series, tuple(ordered), cache_hit=cache_hit)
        finally:
            conn.close()

    def get_latest(self, series_id: str, *, refresh: bool = False) -> QueryResult:
        end = datetime.now(UTC).date()
        conn = db.open_connection(self.db_path)
        try:
            series = db.get_series(conn, series_id)
            cached_latest = db.latest_observation(conn, series_id, on_or_before=end)
            if not refresh and cached_latest is not None:
                max_age = LATEST_CACHE_MAX_AGE_DAYS.get(series.frequency, 370)
                if cached_latest.observed_at >= end - timedelta(days=max_age):
                    return QueryResult(series, (cached_latest,), cache_hit=True)
        finally:
            conn.close()
        lookback_days = LATEST_FETCH_LOOKBACK_DAYS.get(
            series.frequency, DEFAULT_LATEST_LOOKBACK_DAYS
        )
        start = end - timedelta(days=lookback_days)
        result = self.get_range(
            series_id,
            start=start,
            end=end,
            refresh=refresh or cached_latest is not None,
        )
        if not result.observations:
            return result
        return QueryResult(
            result.series,
            (max(result.observations, key=lambda item: item.observed_at),),
            cache_hit=result.cache_hit,
        )

    def _fetch_and_store(
        self,
        conn: sqlite3.Connection,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
        started_at = datetime.now(UTC)
        try:
            observations = _fetch_observations_with_retry(
                series, start=start, end=end, context=context
            )
            db.insert_observations(conn, observations)
            db.record_provider_run(
                conn,
                provider=series.provider,
                series_id=series.series_id,
                start=start,
                end=_provider_run_coverage_end(
                    series, requested_end=end, observations=observations
                ),
                started_at=started_at,
                status="ok",
                record_count=len(observations),
            )
            conn.commit()
            return observations
        except Exception as exc:
            db.record_provider_run(
                conn,
                provider=series.provider,
                series_id=series.series_id,
                start=start,
                end=end,
                started_at=started_at,
                status="failed",
                record_count=0,
                error_message=str(exc),
            )
            conn.commit()
            raise


def _provider_run_coverage_end(
    series: SeriesDefinition,
    *,
    requested_end: date,
    observations: list[ObservationRecord],
) -> date:
    if series.frequency == "daily" and observations:
        return min(requested_end, max(item.observed_at for item in observations))
    return requested_end


def _fetch_observations_with_retry(
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    context: FetchContext | None,
) -> list[ObservationRecord]:
    for attempt in range(1, PROVIDER_FETCH_ATTEMPTS + 1):
        try:
            return fetch_observations(series, start=start, end=end, context=context)
        except IndicatorsProviderError:
            if attempt == PROVIDER_FETCH_ATTEMPTS:
                raise
            time.sleep(PROVIDER_FETCH_RETRY_BACKOFF_SECONDS)
    raise AssertionError("unreachable provider retry loop")
