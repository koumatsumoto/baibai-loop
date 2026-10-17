"""Query-only store freshness lookups for the meta view.

Freshness derives exclusively from timestamps recorded inside each store, never
from file mtime, so the values stay correct after the stores are copied to a
remote object store that does not preserve filesystem metadata.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.collection.economic_sources import DOCUMENT_PROVIDERS
from baibai_engine.foundation.sqlite_read import read_store_if_written
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.read_contracts import (
    NATIVE_PROVIDERS,
    native_source_key,
)
from baibai_engine.market_context.indicators.source_snapshots import decode_records
from baibai_engine.market_context.reading.documents import read_document_series

from .sqlite import connect_read_only, read_application_rows, read_rows

# Judgment-layer stores are all JST-domain records. Date-only columns
# and any timezone-naive value are read at JST
# so they compare with the timezone-aware timestamps in the same max().
_JST = ZoneInfo("Asia/Tokyo")


def screening_latest_asof(path: Path) -> date | None:
    """Return the as-of date of the newest screening run, or None without runs."""

    rows = read_rows(path, "SELECT max(asof_date) FROM screening_run")
    if not rows or rows[0][0] is None:
        return None
    return date.fromisoformat(str(rows[0][0]))


def indicator_latest_observed_at(path: Path) -> date | None:
    """Return the newest successful source observation, excluding old derived caches."""

    if not path.is_file():
        return None
    definitions = load_definitions().series

    def document_latest() -> str | None:
        with closing(connect_read_only(path)) as connection:
            connection.execute("BEGIN")
            return max(
                (
                    value.observed_at.isoformat()
                    for series in definitions
                    if series.provider in DOCUMENT_PROVIDERS
                    for value in read_document_series(
                        connection, series, start=date.min, end=date.max
                    )
                ),
                default=None,
            )

    # Request dates and acquisition times are not observation dates. Use the
    # same document interpretation as the chart, without an old scalar fallback.
    latest = read_store_if_written(path, document_latest)
    native_keys = {
        native_source_key(series) for series in definitions if series.provider in NATIVE_PROVIDERS
    }
    if native_keys:
        native = read_rows(
            path,
            "SELECT c.source_key, max(c.observed_at) FROM indicator_source_snapshots c "
            "WHERE c.record_count > 0 AND c.fetched_at = ("
            "SELECT max(n.fetched_at) FROM indicator_source_snapshots n "
            "WHERE n.source_key = c.source_key AND n.observed_at = c.observed_at) "
            "GROUP BY c.source_key",
        )
        for source_key, observed_at in native:
            if (
                source_key in native_keys
                and source_key != "jquants_flows:TSEPrime"
                and observed_at is not None
            ):
                native_date = str(observed_at)
                latest = max(latest, native_date) if latest is not None else native_date
    if "jquants_flows:TSEPrime" in native_keys:
        # The table key is the publication date for flows; freshness here means
        # the economic observation period, as it does for the scalar series.
        publications = read_rows(
            path,
            "SELECT c.payload FROM indicator_source_snapshots c "
            "WHERE c.source_key = 'jquants_flows:TSEPrime' AND c.fetched_at = ("
            "SELECT max(n.fetched_at) FROM indicator_source_snapshots n "
            "WHERE n.source_key = c.source_key AND n.observed_at = c.observed_at)",
        )
        for (payload,) in publications:
            for record in decode_records(payload):
                period_end = record.get("EnDate")
                if not isinstance(period_end, str):
                    raise ValueError("investor flow has no native EnDate")
                native_date = date.fromisoformat(period_end).isoformat()
                latest = max(latest, native_date) if latest is not None else native_date
    return None if latest is None else date.fromisoformat(latest)


def application_db_updated_at(path: Path) -> datetime | None:
    """Return the latest recorded business timestamp as a freshness proxy.

    This is not the database's last write time: ledger occurrence dates can be
    reported later, task edits and active-session checkpoints have no write-time
    column, and quote updates are not represented here. No additional audit state
    is created for this display value. Saved offsets compare as aware instants;
    date-only and naive values use JST.
    """

    # Read each known source without creating an unwritten store.
    latest: datetime | None = None
    for table, column in _WRITE_INSTANT_COLUMNS:
        rows = read_application_rows(
            path,
            # Fixed pairs from the tuple below; no caller input reaches this string.
            f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL",  # nosec B608
        )
        for row in rows:
            instant = _as_jst_instant(str(row[0]))
            latest = instant if latest is None else max(latest, instant)
    return latest


# Business-record timestamp sources, not an exhaustive write log.
# Nullable timestamps contribute only once set.
_WRITE_INSTANT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ledger_event", "occurred_at"),
    ("stock_assessment", "published_at"),
    ("stock_assessment_review", "reviewed_at"),
    ("investment_proposal", "published_at"),
    ("market_context_assessment", "published_at"),
    ("task", "created_at"),
    ("task", "closed_at"),
    ("operation_session", "started_at"),
    ("operation_session", "completed_at"),
)


def _as_jst_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=_JST)


__all__ = [
    "application_db_updated_at",
    "indicator_latest_observed_at",
    "screening_latest_asof",
]
