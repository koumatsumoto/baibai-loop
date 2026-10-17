"""Maintenance commands for JPX delistings and tender-offer exit values."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

from baibai_engine.collection.edinet.models import EdinetAcquisitionError
from baibai_engine.foundation.time import JST
from baibai_engine.market.delistings import DelistingSourceError
from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.market.jpx_models import JPXProviderError
from baibai_engine.market.jpx_refresh import JPXRefresh
from baibai_engine.market.tender_offer import (
    TenderOfferDocumentReader,
    TenderOfferError,
    TenderOfferExitSummary,
    TenderOfferExitValue,
    build_tender_offer_exit_values,
    derive_tender_offer_exit_values,
    read_tender_offer_blocks,
    store_tender_offer_exit_values,
)
from baibai_engine.storage.observations.schema import SQLiteSchemaError

from .providers import EDINETMaintenance


def refresh_jpx_delistings_command(*, sqlite_path: Path, stdout: TextIO | None = None) -> int:
    """Capture the linked JPX pages and replace the observed view."""
    out = stdout if stdout is not None else sys.stdout
    try:
        with closing(JPXRefresh(sqlite_path=sqlite_path)) as refresh:
            stored = len(refresh.refresh_delistings_snapshot(datetime.now(JST).date()))
    except (
        DelistingSourceError,
        JPXProviderError,
        SQLiteSchemaError,
        OSError,
        sqlite3.Error,
    ) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"refresh-jpx-delistings: rows={stored}", file=out, flush=True)
    return 0


def build_tender_offer_exits_command(
    *,
    sqlite_path: Path,
    asof_date: date,
    reader: TenderOfferDocumentReader,
    stdout: TextIO | None = None,
) -> int:
    """Derive tender-offer-price exit proxies from stored EDINET archives."""
    return _store_and_report_exits(
        sqlite_path=sqlite_path,
        calculate=lambda: build_tender_offer_exit_values(
            sqlite_path, reader=reader, asof=asof_date
        ),
        label="build-tender-offer-exits",
        stdout=stdout,
    )


def refresh_tender_offer_exits_command(
    *,
    sqlite_path: Path,
    asof_date: date,
    reader: TenderOfferDocumentReader,
    acquisition: EDINETMaintenance,
    stdout: TextIO | None = None,
) -> int:
    """Acquire each requested archive before deriving tender-offer exit proxies."""

    def acquire_and_read(doc_id: str) -> Mapping[str, str]:
        acquisition.download_csv_zip(doc_id)
        return read_tender_offer_blocks(reader, doc_id)

    return _store_and_report_exits(
        sqlite_path=sqlite_path,
        calculate=lambda: derive_tender_offer_exit_values(
            sqlite_path, read_blocks=acquire_and_read, asof=asof_date
        ),
        label="refresh-tender-offer-exits",
        stdout=stdout,
    )


def _store_and_report_exits(
    *,
    sqlite_path: Path,
    calculate: Callable[[], tuple[tuple[TenderOfferExitValue, ...], TenderOfferExitSummary]],
    label: str,
    stdout: TextIO | None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    try:
        values, summary = calculate()
        stored = store_tender_offer_exit_values(sqlite_path, values)
    except (TenderOfferError, EdinetAcquisitionError, EDINETProviderError, sqlite3.Error) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(
        f"{label}: "
        f"delistings={summary.delisting_count}, "
        f"candidates={summary.candidate_delisting_count}, "
        f"resolved={stored}, reads={summary.read_document_count}",
        file=out,
        flush=True,
    )
    print(
        f"{label} rejections: "
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
    "build_tender_offer_exits_command",
    "refresh_jpx_delistings_command",
    "refresh_tender_offer_exits_command",
)
