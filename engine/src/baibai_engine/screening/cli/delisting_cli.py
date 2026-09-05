"""Maintenance commands for JPX delistings and tender-offer exit values."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import TextIO

from baibai_engine.screening.delistings import (
    DelistingSourceError,
    download_jpx_delistings,
    store_jpx_delistings,
)
from baibai_engine.screening.providers.edinet import EDINETProviderError
from baibai_engine.screening.tender_offer import (
    TenderOfferError,
    build_tender_offer_exit_values,
    store_tender_offer_exit_values,
)

from .providers import ProviderBundle


def refresh_jpx_delistings_command(*, sqlite_path: Path, stdout: TextIO | None = None) -> int:
    """Download and accumulate the current JPX delisting record."""
    out = stdout if stdout is not None else sys.stdout
    try:
        stored = store_jpx_delistings(sqlite_path, download_jpx_delistings())
    except (DelistingSourceError, OSError, sqlite3.Error) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"refresh-jpx-delistings: rows={stored}", file=out, flush=True)
    return 0


def build_tender_offer_exits_command(
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
        values, summary = build_tender_offer_exit_values(
            sqlite_path, provider=providers.edinet, asof=asof_date
        )
        stored = store_tender_offer_exit_values(sqlite_path, values)
    except (TenderOfferError, EDINETProviderError, sqlite3.Error) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(
        "build-tender-offer-exits: "
        f"delistings={summary.delisting_count}, "
        f"tender_offer_delistings={summary.tender_offer_delisting_count}, "
        f"resolved={stored}, downloads={summary.downloaded_document_count}",
        file=out,
        flush=True,
    )
    print(
        "build-tender-offer-exits rejections: "
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


__all__ = ("build_tender_offer_exits_command", "refresh_jpx_delistings_command")
