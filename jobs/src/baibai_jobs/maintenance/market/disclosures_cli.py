"""Maintenance commands for TSE policy observations."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import TextIO

from baibai_engine.machine_analysis.observations.disclosures.capital_policy import (
    CapitalPolicyError,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import JPXProviderError
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_jobs.maintenance.source_refresh.jpx import JPXRefresh


def refresh_tse_capital_policy_command(
    *,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    """Re-read the TSE capital-policy disclosure workbook."""
    out = stdout if stdout is not None else sys.stdout
    try:
        with closing(JPXRefresh(sqlite_path=sqlite_path)) as refresh:
            summary = refresh.refresh_capital_policy_snapshot(datetime.now(JST).date())
    except (
        JPXProviderError,
        CapitalPolicyError,
        SQLiteSchemaError,
        OSError,
        sqlite3.Error,
    ) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "refresh-tse-capital-policy: "
        f"tse_sheets={summary.sheet_count}, tse_rows={summary.row_count}, "
        f"tse_disclosed={summary.disclosed_count}, "
        f"tse_considering={summary.considering_count}",
        file=out,
        flush=True,
    )
    return 0


__all__ = ("refresh_tse_capital_policy_command",)
