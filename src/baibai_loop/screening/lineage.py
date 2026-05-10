from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


@dataclass(slots=True)
class _SqliteCoverageAccumulator:
    source: str
    windows: int = 0
    records: int = 0
    statuses: set[str] = field(default_factory=set)
    non_ok_windows: int = 0
    errors: set[str] = field(default_factory=set)

    def as_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "windows": self.windows,
            "records": self.records,
            "statuses": sorted(self.statuses),
            "non_ok_windows": self.non_ok_windows,
            "errors": sorted(self.errors),
        }


def build_run_id(asof_date: date) -> str:
    return f"screening-{asof_date:%Y%m%d}"


def compute_sqlite_summary(sqlite_path: Path | None) -> dict[str, object] | None:
    """Return a lightweight current-state summary for diagnostics."""
    if sqlite_path is None or not sqlite_path.exists():
        return None
    conn = sqlite3.connect(sqlite_path)
    try:
        try:
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
            coverage_rows = conn.execute(
                "SELECT source, record_count, status, error "
                "FROM source_coverage ORDER BY source, coverage_key"
            ).fetchall()
        except sqlite3.OperationalError:
            return None
    finally:
        conn.close()

    coverage_by_source: dict[str, _SqliteCoverageAccumulator] = {}
    for source, record_count, status, error in coverage_rows:
        source_key = str(source)
        entry = coverage_by_source.setdefault(
            source_key, _SqliteCoverageAccumulator(source=source_key)
        )
        entry.windows += 1
        entry.records += int(record_count or 0)
        entry.statuses.add(str(status or "ok"))
        if status != "ok":
            entry.non_ok_windows += 1
        if error:
            entry.errors.add(str(error))

    return {
        "path": sqlite_path.as_posix(),
        "user_version": user_version,
        "coverage": [entry.as_payload() for entry in coverage_by_source.values()],
    }


def dumps_sqlite_summary(summary: dict[str, object]) -> str:
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)
