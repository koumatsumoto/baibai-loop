"""Materialize one EDINET list from retained source receipts without network access."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.collection.edinet.receipts import read_list_responses
from baibai_engine.market.edinet_list_projection import (
    DocumentListProjection,
    document_list_rechecked_asof,
    project_document_list,
)
from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.storage.observations.ingest.edinet import store_edinet_document_projection
from baibai_engine.storage.observations.schema import connect_current

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
