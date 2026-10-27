"""旧較正入力の手動coverage診断。source取得は行わない。"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import TextIO

from baibai_engine.machine_analysis.screening.sqlite_coverage import (
    CacheCoverageIssue,
    verify_screening_sqlite_coverage,
)


def verify_cache_coverage_command(
    *,
    sqlite_path: Path,
    asof_date: date,
    required_jpx_sources: Iterable[str] = (),
    allow_stale_jpx: bool = False,
    stdout: TextIO | None = None,
) -> int:
    """Diagnose saved SQLite inputs for the legacy calibration contract.

    This is intentionally local-only: it does not inspect raw JSON files and
    does not call provider APIs. Refresh/rebuild must happen before this check.
    """
    out = stdout if stdout is not None else sys.stdout
    issues = verify_screening_sqlite_coverage(
        sqlite_path,
        asof_date,
        required_jpx_sources=required_jpx_sources,
        allow_stale_jpx=allow_stale_jpx,
    )
    if issues:
        _print_cache_coverage_issues(issues, asof_date=asof_date, stream=out)
        return 1
    print(
        f"SQLite cache coverage complete for --asof {asof_date.isoformat()}: {sqlite_path}",
        file=out,
    )
    return 0


def _print_cache_coverage_issues(
    issues: Sequence[CacheCoverageIssue],
    *,
    asof_date: date,
    stream: TextIO,
) -> None:
    print(f"SQLite cache coverage incomplete for --asof {asof_date.isoformat()}:", file=stream)
    for issue in issues:
        print(f"  {issue.source} {issue.requirement}: {issue.reason}", file=stream)
    print(
        "coverage verification is read-only and does not fetch raw JSON or provider APIs; "
        "repair only the reported source scope through baibai-engine data-collection market "
        "(see jobs/OPERATIONS.md), then rerun this legacy calibration diagnostic.",
        file=stream,
    )
