"""Retained external document bodies and request metadata, without L2 extraction."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from baibai_engine.data_collection.receipts.native_sources.receipt_policy import retain_unchanged


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceDocumentMetadata:
    source_id: str
    request_key: str
    source_url: str
    content_type: str
    fetched_at: datetime
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceDocument(SourceDocumentMetadata):
    payload: bytes


PMI_INPUT_TABLE = "economic_pmi_input_documents"
PMI_INPUT_COLUMNS = (
    "source_id",
    "request_key",
    "fetched_at",
    "source_url",
    "content_type",
    "metadata",
)


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
    for (
        stored_source,
        key,
        fetched_at,
        url,
        content_type,
        payload,
        raw_metadata,
    ) in read_document_rows(connection, source_id):
        yield SourceDocument(
            source_id=stored_source,
            request_key=key,
            source_url=url,
            content_type=content_type,
            fetched_at=datetime.fromisoformat(fetched_at),
            payload=payload,
            metadata=_decode_metadata(raw_metadata),
        )


def read_document_rows(
    connection: sqlite3.Connection, source_id: str
) -> Iterator[tuple[str, str, str, str, str, bytes, str]]:
    """The same latest documents, preserving raw receipt identity and metadata."""
    rows = connection.execute(
        "SELECT c.source_id, c.request_key, c.fetched_at, c.source_url, "
        "c.content_type, c.payload, c.metadata FROM economic_source_documents c "
        "WHERE c.source_id = ? AND c.fetched_at = ("
        "SELECT MAX(n.fetched_at) FROM economic_source_documents n "
        "WHERE n.source_id = c.source_id AND n.request_key = c.request_key) "
        "ORDER BY c.request_key",
        (source_id,),
    )
    # Do not delegate Cursor.close after a consumer has already closed the DB.
    for row in rows:  # noqa: UP028
        yield row


def read_document_metadata(
    connection: sqlite3.Connection, source_id: str
) -> Iterator[SourceDocumentMetadata]:
    """Combine fixed planning metadata with newer real receipts, without reading bodies."""
    tables = ["economic_source_documents"]
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (PMI_INPUT_TABLE,)
    ).fetchone():
        tables.insert(0, PMI_INPUT_TABLE)
    latest: dict[str, SourceDocumentMetadata] = {}
    for table in tables:
        rows = connection.execute(
            f"SELECT c.source_id, c.request_key, c.fetched_at, c.source_url, "  # nosec B608
            f"c.content_type, c.metadata FROM {table} c "
            "WHERE c.source_id = ? AND c.fetched_at = ("
            f"SELECT MAX(n.fetched_at) FROM {table} n "
            "WHERE n.source_id = c.source_id AND n.request_key = c.request_key)",
            (source_id,),
        )
        for stored_source, key, fetched_at, url, content_type, raw_metadata in rows:
            item = SourceDocumentMetadata(
                source_id=stored_source,
                request_key=key,
                source_url=url,
                content_type=content_type,
                fetched_at=datetime.fromisoformat(fetched_at),
                metadata=_decode_metadata(raw_metadata),
            )
            prior = latest.get(key)
            if prior is None or item.fetched_at >= prior.fetched_at:
                latest[key] = item
    yield from (latest[key] for key in sorted(latest))


def _decode_metadata(raw: str) -> Mapping[str, object]:
    metadata = json.loads(raw)
    if not isinstance(metadata, dict):
        raise ValueError("stored source document metadata must be a mapping")
    return metadata
