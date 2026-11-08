"""source入力の取得・再開・coverage保守。モデル生成や候補選定はしない。"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import TextIO

from baibai_engine.machine_analysis.observations.disclosures.jpx_models import JPXProviderError
from baibai_engine.storage.sqlite.source_cache.coverage import (
    count_overlapping_source_coverage,
    count_source_coverage,
    delete_overlapping_source_coverage,
    delete_source_coverage,
    source_coverage_sources,
)
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError, open_connection
from baibai_jobs.maintenance.market.providers import ProviderBundle


def invalidate_coverage_command(
    *,
    sqlite_path: Path,
    source: str,
    start: date | None = None,
    end: date | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Invalidate fetch coverage without deleting the stored observations.

    Sources governed by coverage windows, including daily bars, will be fetched
    again when required. Stored observations alone do not establish acquisition
    coverage. With start/end, only overlapping coverage windows are removed.
    Source retention, the active plan and availability determine whether a later
    fetch can succeed.
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
        conn = open_connection(sqlite_path)
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


def bootstrap_cache_command(
    *,
    asof_date: date,
    providers: ProviderBundle,
    sqlite_path: Path,
    source_group: str,
    stdout: TextIO | None = None,
) -> int:
    groups = {
        "jpx-earnings": _bootstrap_jpx_earnings,
        "jpx-regulations": _bootstrap_jpx_regulations,
    }
    if source_group not in groups:
        raise ValueError("unsupported acquisition source group")
    out = stdout if stdout is not None else sys.stdout
    print(f"bootstrap-cache start: asof={asof_date.isoformat()}", file=out, flush=True)
    try:
        groups[source_group](asof_date, providers, sqlite_path, out)
    except (JPXProviderError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        print("bootstrap-cache incomplete", file=out, flush=True)
        return 1
    print("bootstrap-cache done", file=out, flush=True)
    return 0


def _bootstrap_jpx_earnings(
    asof_date: date, providers: ProviderBundle, _path: Path, out: TextIO
) -> None:
    if providers.jpx_refresh is None:
        raise JPXProviderError("JPX maintenance provider is not configured")
    result = providers.jpx_refresh.refresh_earnings_calendar_snapshot(asof_date)
    print(f"JPX earnings_calendar: {result.valid_record_count} row(s)", file=out, flush=True)


def _bootstrap_jpx_regulations(
    asof_date: date, providers: ProviderBundle, _path: Path, out: TextIO
) -> None:
    if providers.jpx_refresh is None:
        raise JPXProviderError("JPX maintenance provider is not configured")
    result = providers.jpx_refresh.refresh_regulation_snapshot(asof_date)
    print(f"JPX regulations: {len(result.flags_by_ticker)} ticker(s)", file=out, flush=True)
