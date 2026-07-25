from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db
from .db import ObservationRecord
from .definitions import SeriesDefinition
from .providers import (
    FetchContext,
    IndicatorsProviderError,
    fetch_observations,
    provider_spec,
)
from .providers.base import StoreReader

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


@dataclass(frozen=True, slots=True)
class RefreshSuccess:
    series_id: str
    result: QueryResult


@dataclass(frozen=True, slots=True)
class RefreshFailure:
    series_id: str
    message: str


# One series' outcome in a multi-series refresh, so a caller reports every
# failure instead of only the one that stopped the pass.
type RefreshOutcome = RefreshSuccess | RefreshFailure


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
            with FetchContext(store_reader=_store_reader(conn)) as context:
                return self._get_range(
                    conn, series_id, start=start, end=end, refresh=refresh, context=context
                )
        finally:
            conn.close()

    def refresh_all_history(self, series_id: str, *, end: date) -> QueryResult:
        conn = db.open_connection(self.db_path)
        try:
            with FetchContext(store_reader=_store_reader(conn), refetch_stored=True) as context:
                return self._refresh_all_history(conn, series_id, end=end, context=context)
        finally:
            conn.close()

    def refresh_series(
        self,
        series_ids: Sequence[str],
        *,
        start: date | None,
        end: date,
    ) -> list[RefreshOutcome]:
        """Refresh several series in one pass, isolating per-series failures.

        ``start`` is ``None`` for an all-history refresh (each provider's declared
        floor). One store connection and one fetch context serve the whole pass, so
        a bulk source file is downloaded once for every series that maps to it and a
        browser-backed provider pays at most one launch. A failure that belongs to
        one series (unknown series, provider error, invalid range) is captured and
        the pass continues, so one broken source cannot leave the rest of the
        registry stale; a store-level failure (schema, IO) still aborts the pass.
        """

        if start is not None and end < start:
            raise ValueError("--end must be on or after --start")
        outcomes: list[RefreshOutcome] = []
        conn = db.open_connection(self.db_path)
        try:
            with FetchContext(
                store_reader=_store_reader(conn), refetch_stored=start is None
            ) as context:
                for series_id in series_ids:
                    try:
                        result = (
                            self._refresh_all_history(conn, series_id, end=end, context=context)
                            if start is None
                            else self._get_range(
                                conn,
                                series_id,
                                start=start,
                                end=end,
                                refresh=True,
                                context=context,
                            )
                        )
                    except (KeyError, ValueError, IndicatorsProviderError) as exc:
                        outcomes.append(RefreshFailure(series_id, _failure_message(exc)))
                        continue
                    outcomes.append(RefreshSuccess(series_id, result))
        finally:
            conn.close()
        return outcomes

    def _get_range(
        self,
        conn: sqlite3.Connection,
        series_id: str,
        *,
        start: date,
        end: date,
        refresh: bool,
        context: FetchContext,
    ) -> QueryResult:
        series = db.get_series(conn, series_id)
        spec = provider_spec(series.provider)
        point_in_time = spec.point_in_time_vintage
        cache_hit = (not refresh) and db.has_ok_coverage(conn, series_id, start, end)
        if not cache_hit:
            observations = self._fetch_and_store(
                conn, series, start=start, end=end, context=context
            )
        else:
            observations = list(
                db.observations_in_range(conn, series_id, start, end, point_in_time=point_in_time)
            )
        # Providers return observations in source order (ECB FX is newest-first);
        # normalize to ascending observed_at so callers get a stable chronology
        # regardless of cache-hit vs provider-fetch path.
        ordered = sorted(observations, key=lambda item: item.observed_at)
        return QueryResult(series, tuple(ordered), cache_hit=cache_hit)

    def _refresh_all_history(
        self,
        conn: sqlite3.Connection,
        series_id: str,
        *,
        end: date,
        context: FetchContext,
    ) -> QueryResult:
        series = db.get_series(conn, series_id)
        spec = provider_spec(series.provider)
        if spec.all_history_rolling_years is not None:
            start = _years_before(_today_jst(), spec.all_history_rolling_years)
            if end < start:
                raise IndicatorsProviderError(
                    f"all-history end {end.isoformat()} precedes the {spec.name} "
                    f"rolling history floor {start.isoformat()}"
                )
        elif spec.all_history_start is not None:
            start = spec.all_history_start
        else:
            raise IndicatorsProviderError(
                f"all-history start is not configured for provider {series.provider}"
            )
        observations = self._fetch_and_store(
            conn,
            series,
            start=start,
            end=end,
            context=context,
            trim_before_first=spec.trim_before_first,
            remove_other_sources=True,
            replace_requested_range=spec.replace_requested_range,
            require_observations=True,
        )
        ordered = sorted(observations, key=lambda item: item.observed_at)
        return QueryResult(series, tuple(ordered), cache_hit=False)

    def get_latest(self, series_id: str, *, refresh: bool = False) -> QueryResult:
        end = datetime.now(UTC).date()
        conn = db.open_connection(self.db_path)
        try:
            series = db.get_series(conn, series_id)
            spec = provider_spec(series.provider)
            cached_latest = db.latest_observation(
                conn, series_id, on_or_before=end, point_in_time=spec.point_in_time_vintage
            )
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
        context: FetchContext,
        trim_before_first: bool = False,
        remove_other_sources: bool = False,
        replace_requested_range: bool = False,
        require_observations: bool = False,
    ) -> list[ObservationRecord]:
        started_at = datetime.now(UTC)
        try:
            observations = _fetch_observations_with_retry(
                series, start=start, end=end, context=context
            )
            if require_observations and not observations:
                raise IndicatorsProviderError(
                    f"all-history refresh returned no observations for {series.series_id}"
                )
            _reject_non_finite(series, observations)
            if trim_before_first and observations:
                # FRED's current licensed delivery window defines reproducible
                # all-history coverage for a series.
                first_observed_at = min(item.observed_at for item in observations)
                conn.execute(
                    "DELETE FROM observations WHERE series_id = ? AND observed_at < ?",
                    (series.series_id, first_observed_at.isoformat()),
                )
            if remove_other_sources and observations:
                conn.execute(
                    "DELETE FROM observations WHERE series_id = ? AND source_url != ? "
                    "AND observed_at BETWEEN ? AND ? AND substr(vintage_at, 1, 10) <= ?",
                    (
                        series.series_id,
                        series.source_url,
                        start.isoformat(),
                        end.isoformat(),
                        end.isoformat(),
                    ),
                )
            if replace_requested_range and observations:
                first_observed_at = min(item.observed_at for item in observations)
                conn.execute(
                    "DELETE FROM observations WHERE series_id = ? "
                    "AND observed_at BETWEEN ? AND ? AND substr(vintage_at, 1, 10) <= ?",
                    (
                        series.series_id,
                        min(start, first_observed_at).isoformat(),
                        end.isoformat(),
                        end.isoformat(),
                    ),
                )
            db.insert_observations(conn, observations)
            db.delete_unchanged_vintages(conn, series.series_id)
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
            conn.rollback()
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


def _store_reader(conn: sqlite3.Connection) -> StoreReader:
    """Read a stored series through the live connection.

    Derived (local) providers read their input series with this, so they see
    inputs committed earlier in the same pass. spglobal_pmi reads it to skip
    months it already holds.
    """

    def read(series_id: str, start: date, end: date) -> tuple[ObservationRecord, ...]:
        return db.observations_in_range(conn, series_id, start, end)

    return read


def _reject_non_finite(series: SeriesDefinition, observations: list[ObservationRecord]) -> None:
    """Keep NaN / ±inf out of the store.

    ``float("nan")`` and ``float("1e999")`` parse from source text as ordinary
    numbers, and once stored they silently poison every derived computation,
    percentile and JSON export that reads the series. This is the one gate every
    provider passes through, so no provider can introduce one on its own.
    """

    for observation in observations:
        if not math.isfinite(observation.value):
            raise IndicatorsProviderError(
                f"{series.series_id} {observation.observed_at.isoformat()}: "
                f"non-finite value {observation.value}"
            )


def _failure_message(exc: Exception) -> str:
    # KeyError stringifies with quotes around the message; unwrap it so an unknown
    # series reads the same as any other failure.
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _today_jst() -> date:
    return datetime.now(ZoneInfo("Asia/Tokyo")).date()


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
