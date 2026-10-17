"""Materialize EDINET lists from retained source receipts without network access."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

from baibai_engine.collection.edinet.receipts import read_list_responses
from baibai_engine.market.edinet_list_projection import (
    DocumentListProjection,
    document_list_rechecked_asof,
    project_document_list,
)
from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.storage.observations.ingest.edinet import store_edinet_document_projection
from baibai_engine.storage.observations.schema import SQLiteSchemaError, connect_current

_JST = ZoneInfo("Asia/Tokyo")


def rebuild_edinet_document_day(sqlite_path: Path, on_date: date) -> DocumentListProjection:
    """Save current success or failed coverage, then return the deterministic result."""
    if not sqlite_path.exists():
        raise EDINETProviderError("EDINET retained source database is missing")
    connection = connect_current(sqlite_path)
    if connection is None:
        raise EDINETProviderError("EDINET retained source database is unavailable")
    try:
        connection.execute("BEGIN")
        receipts = read_list_responses(connection, doc_date=on_date)
        if not receipts:
            raise EDINETProviderError(f"EDINET list receipts are missing: {on_date.isoformat()}")
        projection = project_document_list(receipts)
        selected = projection.selected_receipt
        rechecked_asof = None
        if projection.error is None and selected is not None:
            capture_day = selected.received_at.astimezone(_JST).date()
            if on_date < capture_day:
                proof = read_list_responses(connection, doc_date=capture_day)
                rechecked_asof = document_list_rechecked_asof(projection, proof)
    finally:
        connection.rollback()
        connection.close()
    store_edinet_document_projection(sqlite_path, projection, rechecked_asof=rechecked_asof)
    return projection


def rebuild_edinet_lists_command(
    *,
    sqlite_path: Path,
    start: date,
    end: date,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Replay a complete retained date range into L2 after a read-only preflight."""
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    if start > end:
        print("--start must be on or before --end", file=errors)
        return 1
    if not sqlite_path.is_file():
        print(f"EDINET retained source database is missing: {sqlite_path}", file=errors)
        return 1
    days: list[date] = []
    current = start
    while True:
        days.append(current)
        if current == end:
            break
        current += timedelta(days=1)
    try:
        connection = connect_current(sqlite_path)
        if connection is None:
            raise EDINETProviderError("EDINET retained source database is unavailable")
        try:
            available = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT doc_date FROM edinet_list_responses "
                    "WHERE doc_date BETWEEN ? AND ?",
                    (start.isoformat(), end.isoformat()),
                )
            }
        finally:
            connection.close()
    except (sqlite3.Error, SQLiteSchemaError, EDINETProviderError) as exc:
        print(f"EDINET list preflight failed: {exc}", file=errors)
        return 1
    missing = [day.isoformat() for day in days if day.isoformat() not in available]
    if missing:
        print("missing raw EDINET list receipts: " + ", ".join(missing), file=errors)
        return 1

    succeeded = failed = projected_rows = 0
    for day in days:
        try:
            projection = rebuild_edinet_document_day(sqlite_path, day)
        except Exception as exc:
            failed += 1
            print(f"{day.isoformat()}: rebuild failed: {exc}", file=errors)
            continue
        if projection.error is not None:
            failed += 1
            print(f"{day.isoformat()}: raw projection failed: {projection.error}", file=errors)
        else:
            succeeded += 1
            projected_rows += len(projection.documents)
    print(
        f"EDINET list rebuild: days_succeeded={succeeded} days_failed={failed} "
        f"projected_rows={projected_rows}",
        file=output,
    )
    return int(failed != 0)
