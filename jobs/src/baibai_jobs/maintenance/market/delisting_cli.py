"""Maintenance commands for JPX delistings and tender-offer exit values."""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TextIO

from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EDINETProviderError,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.edinet_inputs import read_edinet_input
from baibai_engine.machine_analysis.observations.disclosures.delistings import DelistingSourceError
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import JPXProviderError
from baibai_engine.machine_analysis.observations.disclosures.tender_offer import (
    TenderOfferDocumentReader,
    TenderOfferError,
    TenderOfferExitSummary,
    TenderOfferExitValue,
    build_tender_offer_exit_values,
    derive_tender_offer_exit_values,
    read_tender_offer_blocks,
    store_tender_offer_exit_values,
)
from baibai_engine.primitives.time import JST
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_jobs.integration.edinet_list_native import materialize_lists
from baibai_jobs.maintenance.market.providers import EDINETMaintenance
from baibai_jobs.maintenance.source_refresh.jpx import JPXRefresh


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


def build_saved_tender_offer_exits_command(
    *,
    sqlite_path: Path,
    asof_date: date,
    saved_input: Path,
    stdout: TextIO | None = None,
) -> int:
    """Read the explicit saved list/archive pair; never select current source heads."""
    out = stdout if stdout is not None else sys.stdout
    try:
        saved = InputSnapshot.model_validate_json(saved_input.read_bytes())
        if not sqlite_path.exists():
            raise TenderOfferError(f"market store is not readable: {sqlite_path}")
        with (
            closing(
                Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            ) as store,
            TemporaryDirectory(prefix="edinet-tender-offer-") as temporary,
        ):
            fixed = read_edinet_input(store, saved)
            documents = Path(temporary) / "documents.sqlite"
            list_report = materialize_lists(fixed.lists, documents)
            if list_report["status"] != "completed" or list_report["receipt_count"] == 0:
                raise EDINETProviderError("saved EDINET list input is unavailable or failed")

            def read_blocks(doc_id: str) -> Mapping[str, str]:
                try:
                    blocks = read_tender_offer_blocks(fixed.archives.read_csv_zip(doc_id))
                except TenderOfferError as error:
                    # A failed source decode must not retract previously usable exits.
                    raise EDINETProviderError(
                        "saved EDINET tender-offer CSV is unreadable"
                    ) from error
                if not blocks:
                    raise EDINETProviderError("saved EDINET tender-offer CSV is unavailable")
                return blocks

            result = _store_and_report_exits(
                sqlite_path=sqlite_path,
                calculate=lambda: derive_tender_offer_exit_values(
                    sqlite_path,
                    read_blocks=read_blocks,
                    asof=asof_date,
                    edinet_sqlite_path=documents,
                ),
                label="build-tender-offer-exits",
                stdout=out,
            )
            if result == 0:
                print(
                    f"build-tender-offer-exits input: reference={fixed.identity.reference}, "
                    f"selected_at={fixed.selected_at.isoformat()}",
                    file=out,
                    flush=True,
                )
            return result
    except Exception as error:
        print(f"EDINET saved tender-offer build failed: {type(error).__name__}", file=sys.stderr)
        return 1


def refresh_tender_offer_exits_command(
    *,
    sqlite_path: Path,
    asof_date: date,
    acquisition: EDINETMaintenance,
    stdout: TextIO | None = None,
) -> int:
    """Acquire each requested archive before deriving tender-offer exit proxies."""

    def acquire_and_read(doc_id: str) -> Mapping[str, str]:
        # The returned bytes are already retained; a separate reader can predate acquisition.
        return read_tender_offer_blocks(acquisition.download_csv_zip(doc_id))

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
    "build_saved_tender_offer_exits_command",
    "build_tender_offer_exits_command",
    "refresh_jpx_delistings_command",
    "refresh_tender_offer_exits_command",
)
