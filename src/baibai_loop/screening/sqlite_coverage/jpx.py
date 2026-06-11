"""JPX regulation source freshness checks."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from baibai_loop.date_utils import weekday_distance
from baibai_loop.screening.render import JST

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
