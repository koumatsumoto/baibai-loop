"""Retained external document bodies and request metadata, without L2 extraction."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from baibai_engine.data_collection.receipts.native_sources.receipt_policy import retain_unchanged


@dataclass(frozen=True, slots=True)
class SourceDocument:
    source_id: str
    request_key: str
    source_url: str
    content_type: str
    fetched_at: datetime
    payload: bytes
    metadata: Mapping[str, object]


def store_document(connection: sqlite3.Connection, document: SourceDocument) -> bool:
    """Stage one received body; the acquisition caller owns the transaction."""
    if document.fetched_at.tzinfo is None or document.fetched_at.utcoffset() is None:
        raise ValueError("source acquisition time must include a timezone")
    receipt_at = document.fetched_at.astimezone(UTC).isoformat(timespec="microseconds")
    metadata = json.dumps(
        dict(document.metadata), ensure_ascii=False, sort_keys=True, allow_nan=False
    )
    received = (document.source_url, document.content_type, document.payload, metadata)
    receipt = connection.execute(
        "SELECT source_url, content_type, payload, metadata FROM economic_source_documents "
        "WHERE source_id = ? AND request_key = ? AND fetched_at = ?",
        (document.source_id, document.request_key, receipt_at),
    ).fetchone()
    if receipt is not None:
        if tuple(receipt) == received:
            return False
        raise ValueError("conflicting source receipt identity for economic source documents")
    previous = connection.execute(
        "SELECT source_url, content_type, payload, metadata FROM economic_source_documents "
        "WHERE source_id = ? AND request_key = ? ORDER BY fetched_at DESC LIMIT 1",
        (document.source_id, document.request_key),
    ).fetchone()
    if previous is not None and tuple(previous) == received and not retain_unchanged.get():
        return False
    connection.execute(
        "INSERT INTO economic_source_documents "
        "(source_id, request_key, fetched_at, source_url, content_type, payload, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            document.source_id,
            document.request_key,
            receipt_at,
            *received,
        ),
    )
    return True


def read_documents(connection: sqlite3.Connection, source_id: str) -> Iterator[SourceDocument]:
    """Read the latest received body of each request, not a prior usable extraction."""
    rows = connection.execute(
        "SELECT c.source_id, c.request_key, c.fetched_at, c.source_url, "
        "c.content_type, c.payload, c.metadata FROM economic_source_documents c "
        "WHERE c.source_id = ? AND c.fetched_at = ("
        "SELECT MAX(n.fetched_at) FROM economic_source_documents n "
        "WHERE n.source_id = c.source_id AND n.request_key = c.request_key) "
        "ORDER BY c.request_key",
        (source_id,),
    )
    for source_id, key, fetched_at, url, content_type, payload, raw_metadata in rows:
        metadata = json.loads(raw_metadata)
        if not isinstance(metadata, dict):
            raise ValueError("stored source document metadata must be a mapping")
        yield SourceDocument(
            source_id=source_id,
            request_key=key,
            source_url=url,
            content_type=content_type,
            fetched_at=datetime.fromisoformat(fetched_at),
            payload=payload,
            metadata=metadata,
        )
