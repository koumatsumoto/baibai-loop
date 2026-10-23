"""Acquire Collection sources without evaluating any L2 series."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.foundation.redaction import redact_credentials

from . import store
from .documents import read_documents, store_document
from .providers import FetchContext, IndicatorsProviderError, provider_spec
from .providers.base import ProviderFetchStoppedError
from .providers.documents import fetch_source_documents
from .providers.jquants_options import OptionChainSource
from .providers.native import fetch_native_records
from .records import store_snapshot, stored_days
from .refresh_policy import (
    DEFAULT_LATEST_LOOKBACK_DAYS as DEFAULT_LATEST_LOOKBACK_DAYS,
)
from .refresh_policy import (
    LATEST_FETCH_LOOKBACK_DAYS as LATEST_FETCH_LOOKBACK_DAYS,
)
from .refresh_policy import (
    recent_start,
    recovery_ranges,
    source_policy,
)
from .sources import DOCUMENT_PROVIDERS, SourceDefinition, load_sources


@dataclass(frozen=True, slots=True)
class SourceRefreshSuccess:
    source_id: str
    start: date
    end: date
    record_count: int


@dataclass(frozen=True, slots=True)
class SourceRefreshFailure:
    source_id: str
    message: str


type SourceRefreshOutcome = SourceRefreshSuccess | SourceRefreshFailure


def refresh_sources(
    source_ids: Sequence[str],
    *,
    db_path: Path = store.DEFAULT_DB_PATH,
    start: date | None,
    end: date,
    recent: bool = False,
) -> tuple[SourceRefreshOutcome, ...]:
    """Refresh each named source once; None start explicitly requests its history."""
    if recent and start is not None:
        raise ValueError("recent refresh cannot have an explicit start")
    if start is not None and end < start:
        raise ValueError("end must be on or after start")
    catalog = load_sources()
    outcomes: list[SourceRefreshOutcome] = []
    selected = list(dict.fromkeys(source_ids))
    if not any(source_id in catalog for source_id in selected):
        return tuple(
            SourceRefreshFailure(source_id, f"unknown economic source: {source_id}")
            for source_id in selected
        )
    with (
        closing(store.open_connection(db_path)) as connection,
        FetchContext(purpose="rebuild" if start is None and not recent else "refresh") as context,
    ):
        for source_id in selected:
            try:
                if source_id not in catalog:
                    raise ValueError(f"unknown economic source: {source_id}")
                source = catalog[source_id]
                if (
                    recent and source_policy(source).mode == "current_only"
                    and end != datetime.now(ZoneInfo("Asia/Tokyo")).date()
                ):
                    raise ValueError("current-only source cannot recover a historical date")
                first = (
                    recent_start(source, end)
                    if recent
                    else start
                    if start is not None
                    else _history_start(source, end)
                )
                received = _refresh_source(
                    connection, source, start=first, end=end, context=context
                )
                if recent:
                    for missing_start, missing_end in recovery_ranges(connection, source, end):
                        received += _refresh_source(
                            connection, source, start=missing_start, end=missing_end,
                            context=context, recheck_existing=False,
                        )
                        first = min(first, missing_start)
                outcomes.append(SourceRefreshSuccess(source_id, first, end, received))
            except Exception as error:
                outcomes.append(SourceRefreshFailure(source_id, redact_credentials(str(error))))
    return tuple(outcomes)


def _history_start(source: SourceDefinition, end: date) -> date:
    spec = provider_spec(source.provider)
    if spec.all_history_rolling_years is not None:
        today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
        try:
            first = today.replace(year=today.year - spec.all_history_rolling_years)
        except ValueError:
            first = today.replace(year=today.year - spec.all_history_rolling_years, day=28)
    elif spec.all_history_start is not None:
        first = spec.all_history_start
    else:
        raise ValueError(f"all-history start is not configured for source {source.source_id}")
    if end < first:
        raise ValueError(f"all-history end {end} precedes source history floor {first}")
    return first


def _refresh_source(
    connection: sqlite3.Connection,
    source: SourceDefinition,
    *,
    start: date,
    end: date,
    context: FetchContext,
    recheck_existing: bool = True,
) -> int:
    started = datetime.now(UTC)
    received = 0
    try:
        if source.provider in DOCUMENT_PROVIDERS:
            existing = {
                item.request_key: item for item in read_documents(connection, source.source_id)
            }
            for document in fetch_source_documents(
                source,
                start=start,
                end=end,
                context=context,
                existing=existing,
                force=context.purpose == "rebuild",
                recheck_existing=recheck_existing,
            ):
                if document.source_id != source.source_id:
                    raise ValueError(f"provider returned a document for {document.source_id}")
                with connection:
                    store_document(connection, document)
                received += 1
        elif source.provider == "jquants_options":
            known = stored_days(connection, start, end, source_id=source.source_id)
            with ExitStack() as resources:
                client = None
                for offset in range((end - start).days + 1):
                    day = start + timedelta(days=offset)
                    if day.weekday() >= 5:
                        continue
                    if (
                        context.purpose == "rebuild"
                        and day in known
                        and day < end - timedelta(days=14)
                    ):
                        continue
                    if client is None:
                        client = resources.enter_context(closing(OptionChainSource(context)))
                    records = client.fetch(day)
                    with connection:
                        store_snapshot(
                            connection,
                            source_id=source.source_id,
                            observed_at=day,
                            fetched_at=datetime.now(UTC),
                            records=records,
                        )
                    received += len(records)
        else:
            records_by_day = _fetch_with_retry(
                lambda: fetch_native_records(source, start=start, end=end, context=context), context
            )
            fetched_at = datetime.now(UTC)
            count = 0
            with connection:
                for offset in range((end - start).days + 1):
                    day = start + timedelta(days=offset)
                    daily_records = records_by_day.get(day, ())
                    store_snapshot(
                        connection,
                        source_id=source.source_id,
                        observed_at=day,
                        fetched_at=fetched_at,
                        records=daily_records,
                    )
                    count += len(daily_records)
            received = count
    except sqlite3.Error:
        raise
    except Exception as error:
        with connection:
            store.record_source_run(
                connection,
                source_id=source.source_id,
                start=start,
                end=end,
                started_at=started,
                status="failed",
                record_count=received,
                error_message=str(error),
            )
        raise
    with connection:
        store.record_source_run(
            connection,
            source_id=source.source_id,
            start=start,
            end=end,
            started_at=started,
            status="ok",
            record_count=received,
        )
    return received


def _fetch_with_retry[T](operation: Callable[[], T], context: FetchContext) -> T:
    for attempt in range(2):
        try:
            return operation()
        except IndicatorsProviderError as error:
            context.discard_cached_bytes()
            if isinstance(error, ProviderFetchStoppedError) or attempt == 1:
                raise
            time.sleep(1.0)
    raise AssertionError("unreachable provider retry loop")
