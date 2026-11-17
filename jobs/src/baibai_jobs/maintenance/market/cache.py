"""source入力の取得・再開・coverage保守。モデル生成や候補選定はしない。"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import TextIO

from baibai_engine.storage.sqlite.source_cache.coverage import (
    count_overlapping_source_coverage,
    count_source_coverage,
    delete_overlapping_source_coverage,
    delete_source_coverage,
    source_coverage_sources,
)
from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLiteSchemaError,
    validate_current_schema,
)


def invalidate_coverage_command(
    *,
    sqlite_path: Path,
    source: str,
    start: date | None = None,
    end: date | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Invalidate fetch coverage without deleting the stored observations.

    This only removes local SQL coverage claims, not native L1 receipt state.
    In particular, native daily-price acquisition plans from its own fixed head;
    invalidating these rows does not force another provider request.
    With start/end, only overlapping coverage windows are removed.
    """
    out = stdout if stdout is not None else sys.stdout
    if (start is None) != (end is None):
        print("--start and --end must be given together", file=sys.stderr)
        return 1
    if start is not None and end is not None and start > end:
        print(
            f"--start {start.isoformat()} must not be after --end {end.isoformat()}",
            file=sys.stderr,
        )
        return 1
    if not sqlite_path.exists():
        print(f"SQLite cache not found: {sqlite_path}", file=sys.stderr)
        return 1
    try:
        # Invalidation cannot create or rebuild a store, even if it vanished after the check.
        conn = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=rw", uri=True)
        try:
            validate_current_schema(conn)
        except BaseException:
            conn.close()
            raise
    except (SQLiteSchemaError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    try:
        known = source_coverage_sources(conn)
        if source not in known:
            known_list = ", ".join(known) if known else "(none)"
            print(
                f"unknown coverage source {source!r}; known sources: {known_list}",
                file=sys.stderr,
            )
            return 1
        if start is not None and end is not None:
            window = f"{start.isoformat()}..{end.isoformat()}"
            target = count_overlapping_source_coverage(conn, source, start, end)
        else:
            window = "all windows"
            target = count_source_coverage(conn, source)
        print(
            f"invalidate-coverage: removing {target} source_coverage row(s) "
            f"for source={source} ({window})",
            file=out,
        )
        if start is not None and end is not None:
            delete_overlapping_source_coverage(conn, source, start, end)
        else:
            delete_source_coverage(conn, source)
        conn.commit()
    finally:
        conn.close()
    print(
        "invalidate-coverage done; stored observations unchanged",
        file=out,
    )
    return 0
