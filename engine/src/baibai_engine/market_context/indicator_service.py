"""Application orchestration for L1 acquisition and read-time L2 series values."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.collection.economic_documents import read_documents, store_document
from baibai_engine.collection.economic_providers import (
    FetchContext,
    IndicatorsProviderError,
    provider_spec,
)
from baibai_engine.collection.economic_providers.base import ProviderFetchStoppedError
from baibai_engine.collection.economic_providers.documents import fetch_source_documents
from baibai_engine.collection.economic_providers.native import fetch_native_records
from baibai_engine.collection.economic_sources import DOCUMENT_PROVIDERS, require_source
from baibai_engine.foundation.redaction import redact_credentials

from .indicators import db
from .indicators.db import IndicatorsSchemaError
from .indicators.definitions import IndicatorDefinitions, SeriesDefinition, load_definitions
from .indicators.observations import ObservationRecord
from .indicators.read_contracts import NATIVE_BULK_PROVIDERS, native_source_key
from .indicators.source_snapshots import store_snapshot, stored_days
from .option_acquisition import OptionAcquisition
from .reading.documents import read_document_series
from .reading.native import read_native_series
from .reading.reader import build_store_observation_reader

DEFAULT_LATEST_LOOKBACK_DAYS = 370
LATEST_FETCH_LOOKBACK_DAYS = {
    "daily": 14,
    "weekly": 60,
    "monthly": DEFAULT_LATEST_LOOKBACK_DAYS,
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


def list_series(*, category: str | None = None) -> tuple[SeriesDefinition, ...]:
    """登録系列の一覧をstoreへの接続なしで見せる。"""
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
    """登録系列の検索結果をstoreへの接続なしで見せる。"""
    return tuple(
        sorted(
            load_definitions().search(query), key=lambda series: (series.priority, series.series_id)
        )
    )


class IndicatorsService:
    def __init__(self, db_path: Path = db.DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

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
        definitions = load_definitions()
        registered = definitions.by_id()
        if series_id not in registered:
            raise KeyError(f"unknown indicator series: {series_id}")
        if not refresh:
            with closing(db.open_read_only_connection(self.db_path)) as conn:
                conn.execute("BEGIN")
                try:
                    reader = build_store_observation_reader(conn, series=definitions.series)
                    observations = tuple(reader(series_id, start, end))
                finally:
                    conn.rollback()
            return QueryResult(registered[series_id], observations, cache_hit=True)
        with (
            closing(db.open_connection(self.db_path, definitions=definitions)) as conn,
            FetchContext(purpose="refresh") as context,
            closing(OptionAcquisition(conn, context)) as options,
        ):
            return self._get_range(
                conn,
                series_id,
                start=start,
                end=end,
                refresh=True,
                context=context,
                options=options,
            )

    def refresh_all_history(self, series_id: str, *, end: date) -> QueryResult:
        definitions = load_definitions()
        if series_id not in definitions.by_id():
            raise KeyError(f"unknown indicator series: {series_id}")
        conn = db.open_connection(self.db_path, definitions=definitions)
        try:
            _prune_registry_for_refresh(conn, definitions)
            with (
                FetchContext(purpose="rebuild") as context,
                closing(OptionAcquisition(conn, context)) as options,
            ):
                return self._refresh_all_history(
                    conn,
                    series_id,
                    end=end,
                    context=context,
                    options=options,
                )
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
        one series is captured and the pass continues, so one broken source cannot
        leave the rest of the registry stale; a store-level failure (schema, IO)
        still aborts the pass because it invalidates every remaining series.
        """

        if start is not None and end < start:
            raise ValueError("--end must be on or after --start")
        definitions = load_definitions()
        registered_ids = definitions.by_id()
        if not any(series_id in registered_ids for series_id in series_ids):
            return [
                RefreshFailure(series_id, f"unknown indicator series: {series_id}")
                for series_id in series_ids
            ]
        outcomes: list[RefreshOutcome] = []
        conn = db.open_connection(self.db_path, definitions=definitions)
        try:
            _prune_registry_for_refresh(conn, definitions)
            with (
                FetchContext(
                    purpose="rebuild" if start is None else "refresh",
                ) as context,
                closing(OptionAcquisition(conn, context)) as options,
            ):
                for series_id in series_ids:
                    try:
                        result = (
                            self._refresh_all_history(
                                conn, series_id, end=end, context=context, options=options
                            )
                            if start is None
                            else self._get_range(
                                conn,
                                series_id,
                                start=start,
                                end=end,
                                refresh=True,
                                context=context,
                                options=options,
                            )
                        )
                    except (sqlite3.Error, IndicatorsSchemaError):
                        raise
                    except Exception as exc:
                        # Deliberately broad: whatever one series' provider raises —
                        # including a bug in its parser or an unwrapped third-party
                        # error — belongs to that series alone. Narrowing this would
                        # let one unforeseen error type strand every later series.
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
        options: OptionAcquisition,
    ) -> QueryResult:
        series = db.get_series(conn, series_id)
        if series.provider == "derived":
            return _derived_range(conn, series, start=start, end=end)
        if series.provider in NATIVE_BULK_PROVIDERS:
            return self._native_range(
                conn, series, start=start, end=end, refresh=refresh, context=context
            )
        if series.provider == "jquants_options":
            observations, cache_hit = options.query(series, start=start, end=end, refresh=refresh)
            return QueryResult(series, observations, cache_hit=cache_hit)
        if series.provider in DOCUMENT_PROVIDERS:
            return self._document_range(
                conn, series, start=start, end=end, context=context, force=False
            )
        raise ValueError(f"unsupported economic source provider: {series.provider}")

    def _refresh_all_history(
        self,
        conn: sqlite3.Connection,
        series_id: str,
        *,
        end: date,
        context: FetchContext,
        options: OptionAcquisition,
    ) -> QueryResult:
        series = db.get_series(conn, series_id)
        if series.provider == "derived":
            return _derived_range(conn, series, start=date(1970, 1, 1), end=end)
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
        if series.provider in NATIVE_BULK_PROVIDERS:
            return self._native_range(
                conn,
                series,
                start=start,
                end=end,
                refresh=True,
                context=context,
                require_observations=True,
            )
        if series.provider == "jquants_options":
            observations, cache_hit = options.query(
                series, start=start, end=end, refresh=True, require_observations=True
            )
            return QueryResult(series, observations, cache_hit=cache_hit)
        if series.provider in DOCUMENT_PROVIDERS:
            return self._document_range(
                conn, series, start=start, end=end, context=context, force=True
            )
        raise ValueError(f"unsupported economic source provider: {series.provider}")

    def get_latest(self, series_id: str, *, refresh: bool = False) -> QueryResult:
        end = _today_jst()
        registered = load_definitions().by_id()
        if series_id not in registered:
            raise KeyError(f"unknown indicator series: {series_id}")
        series = registered[series_id]
        start = (
            date(1970, 1, 1)
            if series.provider == "derived"
            else end
            - timedelta(
                days=LATEST_FETCH_LOOKBACK_DAYS.get(series.frequency, DEFAULT_LATEST_LOOKBACK_DAYS)
            )
        )
        result = self.get_range(series_id, start=start, end=end, refresh=refresh)
        return QueryResult(result.series, result.observations[-1:], result.cache_hit)

    def _native_range(
        self,
        conn: sqlite3.Connection,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        refresh: bool,
        context: FetchContext,
        require_observations: bool = False,
    ) -> QueryResult:
        source_key = native_source_key(series)
        known = stored_days(conn, start, end, source_key=source_key)
        cache_hit = not refresh and len(known) == (end - start).days + 1
        if not cache_hit:
            started_at = datetime.now(UTC)
            try:
                records = _fetch_with_retry(
                    lambda: fetch_native_records(
                        require_source(series.source_id), start=start, end=end, context=context
                    ),
                    context=context,
                )
                fetched_at = datetime.now(UTC)
                record_count = 0
                # Acquisition commits independently of L2 projection. A missing
                # output column must not turn successfully retained input into a
                # provider failure or trigger a needless external reacquisition.
                with conn:
                    for offset in range((end - start).days + 1):
                        day = start + timedelta(days=offset)
                        rows = records.get(day, ())
                        store_snapshot(
                            conn,
                            source_key=source_key,
                            observed_at=day,
                            fetched_at=fetched_at,
                            records=rows,
                        )
                        record_count += len(rows)
                    db.record_provider_run(
                        conn,
                        provider=series.provider,
                        series_id=series.series_id,
                        start=start,
                        end=end,
                        started_at=started_at,
                        status="ok",
                        record_count=record_count,
                    )
            except sqlite3.Error:
                raise
            except Exception as error:
                with conn:
                    db.record_provider_run(
                        conn,
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
        observations = read_native_series(conn, series, start=start, end=end)
        if require_observations and not observations:
            raise ValueError(f"retained inputs produced no observations for {series.series_id}")
        return QueryResult(series, observations, cache_hit=cache_hit)

    def _document_range(
        self,
        conn: sqlite3.Connection,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        context: FetchContext,
        force: bool,
    ) -> QueryResult:
        source = require_source(series.source_id)
        existing = {item.request_key: item for item in read_documents(conn, source.source_id)}
        started_at = datetime.now(UTC)
        received = 0
        try:
            for document in fetch_source_documents(
                source, start=start, end=end, context=context, existing=existing, force=force
            ):
                if document.source_id != source.source_id:
                    raise ValueError(f"provider returned a document for {document.source_id}")
                # A later source failure must not destroy an already received PDF.
                with conn:
                    store_document(conn, document)
                received += 1
        except sqlite3.Error:
            raise
        except Exception as error:
            with conn:
                db.record_provider_run(
                    conn,
                    provider=source.provider,
                    series_id=series.series_id,
                    start=start,
                    end=end,
                    started_at=started_at,
                    status="failed",
                    record_count=received,
                    error_message=redact_credentials(str(error)),
                )
            raise
        with conn:
            db.record_provider_run(
                conn,
                provider=source.provider,
                series_id=series.series_id,
                start=start,
                end=end,
                started_at=started_at,
                status="ok",
                record_count=received,
            )
        # L2 errors cannot relabel successful receipt as a source download failure.
        observations = read_document_series(conn, series, start=start, end=end)
        return QueryResult(series, observations, cache_hit=False)


def _derived_range(
    conn: sqlite3.Connection, series: SeriesDefinition, *, start: date, end: date
) -> QueryResult:
    """Read one consistent input snapshot without persisting recomputable values."""
    conn.execute("BEGIN")
    try:
        reader = build_store_observation_reader(conn, series=load_definitions().series)
        values = tuple(reader(series.series_id, start, end))
        return QueryResult(series, values, cache_hit=True)
    finally:
        conn.rollback()


def _prune_registry_for_refresh(
    conn: sqlite3.Connection,
    definitions: IndicatorDefinitions,
) -> None:
    """Commit registry pruning before reporting deleted row counts."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        stored_generation = db.registry_generation(conn)
        if stored_generation > definitions.generation:
            raise ValueError(
                "indicator registry is newer than this client "
                f"(store={stored_generation}, client={definitions.generation}); "
                "refusing refresh"
            )
        unregistered = db.unregistered_series_ids(conn, definitions)
        if unregistered and stored_generation == definitions.generation:
            # A membership change requires a higher generation. Facts written
            # by another membership at this generation are not ours to delete.
            raise ValueError(
                "indicator store holds series this registry does not name at the same "
                f"generation (generation={stored_generation}, "
                f"unregistered={', '.join(unregistered)}); refusing refresh"
            )
        pruned = db.prune_definitions(conn, definitions)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    if pruned:
        message = "\n".join(
            f"registry-prune\t{result.series_id}\t"
            f"observations={result.observation_rows}\t"
            f"provider_runs={result.provider_run_rows}"
            for result in pruned
        )
        # A reporting failure cannot undo the completed transaction.
        with suppress(OSError):
            print(message, flush=True)


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


def _fetch_with_retry[T](operation: Callable[[], T], *, context: FetchContext | None) -> T:
    for attempt in range(1, PROVIDER_FETCH_ATTEMPTS + 1):
        try:
            return operation()
        except IndicatorsProviderError as exc:
            # The response that produced this failure may be cached (a block page
            # arrives as HTTP 200), so drop the cache before retrying. Otherwise the
            # retry re-reads the same bytes and every later series sharing the URL
            # inherits the failure.
            if context is not None:
                context.discard_cached_bytes()
            if isinstance(exc, ProviderFetchStoppedError) or attempt == PROVIDER_FETCH_ATTEMPTS:
                raise
            time.sleep(PROVIDER_FETCH_RETRY_BACKOFF_SECONDS)
    raise AssertionError("unreachable provider retry loop")
