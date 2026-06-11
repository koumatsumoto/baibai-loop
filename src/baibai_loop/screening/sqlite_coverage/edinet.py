"""EDINET metrics coverage checks."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from .shared import CacheCoverageIssue


def _append_edinet_metrics_coverage_issues(
    conn: sqlite3.Connection,
    issues: list[CacheCoverageIssue],
    *,
    asof_date: date,
) -> None:
    rows = conn.execute(
        "SELECT ticker, failure_reasons FROM edinet_metrics WHERE asof_date = ?",
        (asof_date.isoformat(),),
    ).fetchall()
    if not rows:
        issues.append(
            CacheCoverageIssue(
                source="edinet_metrics",
                requirement=asof_date.isoformat(),
                reason="EDINET metrics are required but not covered in SQLite",
            )
        )
        return
    for ticker, failure_reasons in rows:
        if not ticker:
            issues.append(
                CacheCoverageIssue(
                    source="edinet_metrics",
                    requirement=asof_date.isoformat(),
                    reason="EDINET metrics contain a row without ticker; repair SQLite",
                )
            )
            return
        if failure_reasons in (None, ""):
            continue
        try:
            parsed = json.loads(str(failure_reasons))
        except json.JSONDecodeError as exc:
            issues.append(
                CacheCoverageIssue(
                    source="edinet_metrics",
                    requirement=asof_date.isoformat(),
                    reason=f"EDINET metrics coverage query failed: JSONDecodeError: {exc}",
                )
            )
            return
        if not isinstance(parsed, list):
            issues.append(
                CacheCoverageIssue(
                    source="edinet_metrics",
                    requirement=asof_date.isoformat(),
                    reason="EDINET metrics failure_reasons is not a JSON list; repair SQLite",
                )
            )
            return
