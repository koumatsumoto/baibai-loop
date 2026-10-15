"""Query-only store freshness lookups for the meta view.

Freshness derives exclusively from timestamps recorded inside each store, never
from file mtime, so the values stay correct after the stores are copied to a
remote object store that does not preserve filesystem metadata.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.market_context.indicators.definitions import load_definitions

from .sqlite import read_application_rows, read_rows

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
    registered = {
        series.series_id for series in definitions
        if series.provider not in {"derived", "jquants_options"}
    }
    rows = read_rows(
        path,
        (
            # A later retraction hides this observation. An even later successful
            # vintage remains eligible, matching the series reader's semantics.
            "SELECT o.series_id, max(o.observed_at) FROM observations o "
            "WHERE o.fetch_status = 'ok' AND NOT EXISTS ("
            "SELECT 1 FROM observations r "
            "WHERE r.series_id = o.series_id AND r.observed_at = o.observed_at "
            "AND r.fetch_status = 'retracted' AND r.vintage_at > o.vintage_at"
            ") GROUP BY o.series_id"
        ),
    )
    latest = max(
        (str(row[1]) for row in rows if str(row[0]) in registered and row[1] is not None),
        default=None,
    )
    if any(series.provider == "jquants_options" for series in definitions):
        native = read_rows(
            path,
            "SELECT max(c.trade_date) FROM jquants_option_chains c "
            "WHERE c.record_count > 0 AND c.fetched_at = ("
            "SELECT max(n.fetched_at) FROM jquants_option_chains n "
            "WHERE n.trade_date = c.trade_date)",
        )
        if native and native[0][0] is not None:
            native_date = str(native[0][0])
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
