"""Maintenance commands for TSE policy and EDINET valuation-catalyst facts."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import TextIO

from baibai_engine.screening.providers.edinet import EDINETProviderError
from baibai_engine.screening.valuation_catalysts import (
    ValuationCatalystError,
    refresh_tse_capital_policy,
)

from .providers import ProviderBundle


def refresh_tse_capital_policy_command(
    *,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    """Re-read the TSE capital-policy disclosure workbook."""
    out = stdout if stdout is not None else sys.stdout
    try:
        summary = refresh_tse_capital_policy(sqlite_path)
    except (ValuationCatalystError, OSError, sqlite3.Error) as exc:
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


def backfill_edinet_identity_command(
    *,
    start: date,
    end: date,
    providers: ProviderBundle,
    stdout: TextIO | None = None,
) -> int:
    """Re-list a window of EDINET days so its rows carry submitter and target codes."""
    out = stdout if stdout is not None else sys.stdout
    if providers.edinet is None:
        print("EDINET provider is not configured", file=sys.stderr)
        return 1
    if start > end:
        print(f"--start {start.isoformat()} is after --end {end.isoformat()}", file=sys.stderr)
        return 1
    try:
        result = providers.edinet.backfill_document_identity(start, end)
    except (EDINETProviderError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        f"backfill-edinet-identity: {start.isoformat()}..{end.isoformat()}, "
        + ", ".join(f"{key}={value}" for key, value in sorted(result.items())),
        file=out,
        flush=True,
    )
    return 0


__all__ = (
    "backfill_edinet_identity_command",
    "refresh_tse_capital_policy_command",
)
