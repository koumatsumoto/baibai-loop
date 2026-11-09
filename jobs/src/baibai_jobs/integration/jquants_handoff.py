"""Export retained J-Quants packets and verify their completed native cutover."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    verify_migrated_calendar_receipts,
)
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsSource
from baibai_engine.data_collection.providers.jquants.margin_alerts_native import (
    verify_migrated_margin_alert_receipts,
)
from baibai_engine.data_collection.providers.jquants.margin_interest_native import (
    verify_migrated_margin_interest_receipts,
)
from baibai_engine.data_collection.providers.jquants.master_native import (
    verify_migrated_master_receipts,
)
from baibai_engine.data_collection.providers.jquants.price_native import (
    verify_migrated_price_receipts,
)
from baibai_engine.data_collection.providers.jquants.receipts import import_requests
from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    verify_migrated_short_sale_receipts,
)
from baibai_engine.storage.sqlite.source_cache.schema import (
    open_connection,
    validate_current_schema,
)
from baibai_jobs.integration.replay_result import ReplayResult

MARKET_SOURCES: tuple[JQuantsSource, ...] = (
    "master",
    "daily_bars",
    "market_calendar",
    "margin_interest",
    "margin_alerts",
    "short_sale_reports",
)


def export_handoff(
    source: Path, output: Path, *, since: datetime, sources: Sequence[JQuantsSource]
) -> tuple[str, ...]:
    if output.exists():
        raise FileExistsError(output)
    if since.tzinfo is None:
        raise ValueError("J-Quants handoff cutoff requires a timezone")
    allowed = set(MARKET_SOURCES)
    if not sources or not set(sources) <= allowed:
        raise ValueError("unsupported J-Quants handoff sources")
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original,
        closing(open_connection(output)) as target,
        target,
    ):
        validate_current_schema(original)
        original.execute("BEGIN")
        selected = tuple(
            sorted(
                {
                    row[0]
                    for name in sources
                    for row in original.execute(
                        "SELECT DISTINCT request_id FROM jquants_source_pages "
                        "WHERE source = ? AND received_at_utc >= ?",
                        (name, since.astimezone(UTC).isoformat(timespec="microseconds")),
                    )
                }
            )
        )
        import_requests(original, target, selected)
    return selected


def validate_source_handoff(
    original: sqlite3.Connection, *, sources: Sequence[JQuantsSource]
) -> None:
    """Accept either the native-page transport or the current full source store."""
    if not sources or not set(sources) <= set(MARKET_SOURCES):
        raise ValueError("unsupported J-Quants handoff sources")
    tables = {
        row[0]
        for row in original.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if set(sources) == set(MARKET_SOURCES) and tables == {"jquants_source_pages"}:
        columns = [
            (row[1], row[2], row[3], row[5])
            for row in original.execute("PRAGMA table_info(jquants_source_pages)")
        ]
        if columns != [
            ("request_id", "TEXT", 1, 1),
            ("page_index", "INTEGER", 1, 2),
            ("source", "TEXT", 1, 0),
            ("query_json", "TEXT", 1, 0),
            ("received_at_utc", "TEXT", 1, 0),
            ("received_on", "TEXT", 1, 0),
            ("body_gzip", "BLOB", 1, 0),
        ]:
            raise ValueError("invalid source-only receipt schema")
    else:
        validate_current_schema(original)


def import_handoff(
    source: Path,
    target: Path,
    request_ids: Sequence[str],
    *,
    sources: Sequence[JQuantsSource],
    master_fingerprints: Mapping[str, str] | None = None,
    calendar_fingerprints: Mapping[str, str] | None = None,
    price_fingerprints: Mapping[str, str] | None = None,
    margin_alerts_fingerprints: Mapping[str, str] | None = None,
    short_sale_reports_fingerprints: Mapping[str, str] | None = None,
    margin_interest_fingerprints: Mapping[str, str] | None = None,
) -> ReplayResult:
    """Accept old packets only when every receipt is already retained by native L1.

    Daily, resume and manual replay all supply the native cutover fingerprints.
    Saved packets remain readable evidence, not an alternate L2 projection path.
    """
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original,
        closing(open_connection(target)),
    ):
        validate_source_handoff(original, sources=sources)
        retained_ids = {
            row[0]
            for row in original.execute("SELECT DISTINCT request_id FROM jquants_source_pages")
        }
        if retained_ids != set(request_ids):
            raise ValueError("J-Quants handoff request list does not cover its original pages")
        migrated_master = (
            verify_migrated_master_receipts(original, request_ids, master_fingerprints)
            if master_fingerprints is not None
            else set()
        )
        migrated_calendar = (
            verify_migrated_calendar_receipts(original, request_ids, calendar_fingerprints)
            if calendar_fingerprints is not None
            else set()
        )
        migrated_price = (
            verify_migrated_price_receipts(original, request_ids, price_fingerprints)
            if price_fingerprints is not None
            else set()
        )
        migrated_alerts = (
            verify_migrated_margin_alert_receipts(original, request_ids, margin_alerts_fingerprints)
            if margin_alerts_fingerprints is not None
            else set()
        )
        migrated_shorts = (
            verify_migrated_short_sale_receipts(
                original, request_ids, short_sale_reports_fingerprints
            )
            if short_sale_reports_fingerprints is not None
            else set()
        )
        migrated_interest = (
            verify_migrated_margin_interest_receipts(
                original, request_ids, margin_interest_fingerprints
            )
            if margin_interest_fingerprints is not None
            else set()
        )
        migrated = (
            migrated_master
            | migrated_calendar
            | migrated_price
            | migrated_alerts
            | migrated_shorts
            | migrated_interest
        )
        if migrated != set(request_ids):
            raise ValueError("J-Quants handoff contains receipts without native cutover proof")
    return ReplayResult(
        report={
            "raw_pages_added": 0,
            **(
                {"migrated_margin_interest_requests": sorted(migrated_interest)}
                if margin_interest_fingerprints is not None
                else {}
            ),
            "replayed_requests": [],
            "incomplete_requests": [],
            **(
                {"migrated_margin_alerts_requests": sorted(migrated_alerts)}
                if margin_alerts_fingerprints is not None
                else {}
            ),
            **(
                {"migrated_short_sale_reports_requests": sorted(migrated_shorts)}
                if short_sale_reports_fingerprints is not None
                else {}
            ),
            **(
                {"migrated_price_requests": sorted(migrated_price)}
                if price_fingerprints is not None
                else {}
            ),
            **(
                {"migrated_calendar_requests": sorted(migrated_calendar)}
                if calendar_fingerprints is not None
                else {}
            ),
            **(
                {"migrated_master_requests": sorted(migrated_master)}
                if master_fingerprints is not None
                else {}
            ),
        },
        complete=True,
    )
