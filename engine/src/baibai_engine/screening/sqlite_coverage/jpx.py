"""JPX regulation source freshness checks."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from baibai_engine.foundation.date_utils import weekday_distance
from baibai_engine.foundation.time import JST

from .shared import CacheCoverageIssue

_JPX_MAX_FETCH_AGE_BUSINESS_DAYS = 7


def _jpx_source_names(conn: sqlite3.Connection, asof_date: date) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT source_name FROM jpx_regulation_sources WHERE asof_date = ?",
        (asof_date.isoformat(),),
    ).fetchall()
    return {str(row[0]) for row in rows if row[0]}


def _append_jpx_freshness_issues(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    asof_date: date,
    allow_stale_jpx: bool,
) -> None:
    if allow_stale_jpx:
        return
    rows = conn.execute(
        "SELECT source_name, fetched_at_utc FROM jpx_regulation_sources WHERE asof_date = ?",
        (asof_date.isoformat(),),
    ).fetchall()
    if not rows:
        issues.append(
            CacheCoverageIssue(
                source="jpx_regulation_flags",
                requirement=asof_date.isoformat(),
                reason="JPX regulation snapshot has no fetched_at_utc; refetch SQLite",
            )
        )
        return
    stale_values: list[str] = []
    invalid_values: list[str] = []
    missing_values: list[str] = []
    for source_name, fetched_at_text in rows:
        if fetched_at_text is None:
            missing_values.append(str(source_name))
            continue
        try:
            fetched_at = datetime.fromisoformat(str(fetched_at_text).replace("Z", "+00:00"))
        except ValueError:
            invalid_values.append(str(fetched_at_text))
            continue
        fetched_date = fetched_at.astimezone(JST).date()
        if weekday_distance(asof_date, fetched_date) > _JPX_MAX_FETCH_AGE_BUSINESS_DAYS:
            stale_values.append(str(fetched_at_text))
    if missing_values:
        issues.append(
            CacheCoverageIssue(
                source="jpx_regulation_flags",
                requirement=asof_date.isoformat(),
                reason="JPX regulation snapshot has source rows without fetched_at_utc: "
                + ", ".join(sorted(missing_values)),
            )
        )
    if invalid_values:
        issues.append(
            CacheCoverageIssue(
                source="jpx_regulation_flags",
                requirement=asof_date.isoformat(),
                reason="JPX regulation snapshot has invalid fetched_at_utc: "
                + ", ".join(sorted(invalid_values)),
            )
        )
    if stale_values:
        issues.append(
            CacheCoverageIssue(
                source="jpx_regulation_flags",
                requirement=asof_date.isoformat(),
                reason=(
                    "JPX regulation snapshot fetched_at_utc is stale for asof; "
                    f"refetch SQLite or pass --allow-stale-jpx: {', '.join(sorted(stale_values))}"
                ),
            )
        )


def _append_jpx_earnings_calendar_issues(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    asof_date: date,
    allow_stale_jpx: bool,
) -> None:
    source = "jpx_earnings_calendar"
    requirement = f"fresh snapshot with a known date on/after {asof_date.isoformat()}"
    rows = conn.execute(
        "SELECT coverage_key, coverage_start, coverage_end, fetched_at_utc, "
        "record_count, status, error "
        "FROM source_coverage WHERE source = ?",
        (source,),
    ).fetchall()
    if len(rows) != 1:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason="JPX earnings calendar must have exactly one logical snapshot coverage row",
            )
        )
        return
    (
        coverage_key,
        coverage_start,
        coverage_end,
        fetched_at_text,
        record_count,
        status,
        error,
    ) = rows[0]
    if coverage_key != "get_earnings_calendar_snapshot:current":
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason="JPX earnings calendar has an unexpected snapshot coverage key",
            )
        )
    if status != "ok":
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason=f"source_coverage status is not ok: {status}: {error or ''}",
            )
        )
    table_count = int(conn.execute("SELECT COUNT(*) FROM jpx_earnings_calendar").fetchone()[0] or 0)
    if table_count <= 0 or table_count != int(record_count or 0):
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason=(
                    f"compatibility table row count ({table_count}) does not match "
                    f"snapshot record_count ({int(record_count or 0)})"
                ),
            )
        )
    actual_range = conn.execute(
        "SELECT MIN(announcement_date), MAX(announcement_date) FROM jpx_earnings_calendar"
    ).fetchone()
    actual_start_text, actual_end_text = actual_range
    try:
        actual_start = date.fromisoformat(str(actual_start_text))
        actual_max = date.fromisoformat(str(actual_end_text))
    except ValueError:
        actual_start = date.min
        actual_max = date.min
    if (
        str(coverage_start) != actual_start.isoformat()
        or str(coverage_end) != actual_max.isoformat()
    ):
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason="JPX earnings calendar coverage dates do not match stored rows",
            )
        )
    if actual_max < asof_date:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason="JPX earnings calendar snapshot contains only past known dates",
            )
        )
    if allow_stale_jpx:
        return
    try:
        fetched_at = datetime.fromisoformat(str(fetched_at_text).replace("Z", "+00:00"))
    except ValueError:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason="JPX earnings calendar has invalid fetched_at_utc",
            )
        )
        return
    if weekday_distance(asof_date, fetched_at.astimezone(JST).date()) > 7:
        issues.append(
            CacheCoverageIssue(
                source=source,
                requirement=requirement,
                reason=(
                    "JPX earnings calendar fetched_at_utc is stale; refetch SQLite or pass "
                    "--allow-stale-jpx"
                ),
            )
        )
        return
