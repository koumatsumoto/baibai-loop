"""Maintenance commands for the capital-policy and control-event fact layer."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import TextIO

from baibai_engine.screening.capital_control import (
    CapitalControlError,
    edinet_event_counts,
    refresh_capital_control_facts,
)
from baibai_engine.screening.providers.edinet import EDINETProviderError
from baibai_engine.screening.tender_offer import (
    TenderOfferError,
    build_control_event_exit_values,
    store_tender_offer_exit_values,
)

from .providers import ProviderBundle


def refresh_capital_control_command(
    *,
    sqlite_path: Path,
    asof_date: date,
    stdout: TextIO | None = None,
) -> int:
    """Re-read the TSE disclosure workbook and the JPX delisting record."""
    out = stdout if stdout is not None else sys.stdout
    try:
        summary = refresh_capital_control_facts(sqlite_path)
    except (CapitalControlError, OSError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    counts = edinet_event_counts(sqlite_path, through=asof_date)
    print(
        "refresh-capital-control: "
        f"tse_sheets={summary.tse_sheet_count}, tse_rows={summary.tse_row_count}, "
        f"tse_disclosed={summary.tse_disclosed_count}, "
        f"tse_considering={summary.tse_considering_count}, "
        f"jpx_delistings={summary.jpx_delisting_count}",
        file=out,
        flush=True,
    )
    print(
        "refresh-capital-control edinet events: "
        + ", ".join(f"{code}={count}" for code, count in sorted(counts.items())),
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


def build_control_event_exits_command(
    *,
    sqlite_path: Path,
    asof_date: date,
    providers: ProviderBundle,
    stdout: TextIO | None = None,
) -> int:
    """Derive realized tender-offer exit values for delisted names."""
    out = stdout if stdout is not None else sys.stdout
    if providers.edinet is None:
        print("EDINET provider is not configured", file=sys.stderr)
        return 1
    try:
        values, summary = build_control_event_exit_values(
            sqlite_path, provider=providers.edinet, asof=asof_date
        )
        stored = store_tender_offer_exit_values(sqlite_path, values)
    except (TenderOfferError, EDINETProviderError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "build-control-event-exits: "
        f"delistings={summary.delisting_count}, "
        f"tender_offer_delistings={summary.tender_offer_delisting_count}, "
        f"resolved={stored}, downloads={summary.downloaded_document_count}",
        file=out,
        flush=True,
    )
    print(
        "build-control-event-exits rejections: "
        + (
            ", ".join(
                f"{reason}={count}" for reason, count in summary.rejection_reason_counts.items()
            )
            or "none"
        ),
        file=out,
        flush=True,
    )
    return 0


__all__ = (
    "backfill_edinet_identity_command",
    "build_control_event_exits_command",
    "refresh_capital_control_command",
)
