from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import cast

from .db import open_connection


@dataclass(frozen=True)
class CoverageFinding:
    indicator_id: str
    message: str


@dataclass(frozen=True)
class CoverageResult:
    kind: str
    start: date
    end: date
    findings: tuple[CoverageFinding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings


def check_coverage(db_path: Path, *, kind: str, start: date, end: date) -> CoverageResult:
    conn = open_connection(db_path)
    findings: list[CoverageFinding] = []
    try:
        requirements = conn.execute(
            "SELECT indicator_id, max_staleness_days FROM coverage_requirements "
            "WHERE kind = ? AND required = 1 ORDER BY indicator_id",
            (kind,),
        ).fetchall()
        if not requirements:
            findings.append(
                CoverageFinding(kind, f"no coverage requirements configured for {kind}")
            )
        for requirement in requirements:
            indicator_id = str(requirement["indicator_id"])
            max_staleness_days = int(requirement["max_staleness_days"])
            if _latest_observation(conn, indicator_id, end, max_staleness_days) is None:
                earliest = end - timedelta(days=max_staleness_days)
                findings.append(
                    CoverageFinding(
                        indicator_id,
                        (
                            f"missing ok observation for {indicator_id} "
                            f"between {earliest.isoformat()} and {end.isoformat()}"
                        ),
                    )
                )
    finally:
        conn.close()
    return CoverageResult(kind=kind, start=start, end=end, findings=tuple(findings))


def _latest_observation(
    conn: sqlite3.Connection,
    indicator_id: str,
    end: date,
    max_staleness_days: int,
) -> sqlite3.Row | None:
    earliest = end - timedelta(days=max_staleness_days)
    row = conn.execute(
        "SELECT * FROM indicator_observations "
        "WHERE indicator_id = ? "
        "AND fetch_status = 'ok' "
        "AND date(COALESCE(as_of_date, period_end, release_date)) BETWEEN date(?) AND date(?) "
        "ORDER BY date(COALESCE(as_of_date, period_end, release_date)) DESC, vintage_at DESC "
        "LIMIT 1",
        (indicator_id, earliest.isoformat(), end.isoformat()),
    ).fetchone()
    return cast(sqlite3.Row | None, row)
