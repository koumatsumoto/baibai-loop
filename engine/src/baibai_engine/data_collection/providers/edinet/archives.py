"""Retained EDINET archive bytes in a caller-selected source store."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from baibai_engine.storage.sqlite.source_cache.schema import validate_current_schema
from datetime import date


def read_source_archive(
    connection: sqlite3.Connection, doc_id: str, source_type: int
) -> bytes | None:
    row = connection.execute(
        "SELECT zip_content FROM edinet_source_archives WHERE doc_id = ? AND source_type = ?",
        (doc_id, source_type),
    ).fetchone()
    return bytes(row[0]) if row is not None else None


def retain_source_archive(
    connection: sqlite3.Connection,
    doc_id: str,
    source_type: int,
    content: bytes,
    *,
    stored_on: date,
) -> bytes:
    """Commit one retained source without committing another caller's pending work.

    The first cached bytes win for this identity. This is not a claim that the
    provider's archive can never change; explicit fresh acquisition is separate.
    """
    if connection.in_transaction:
        raise ValueError("EDINET archive retention requires an idle connection")
    try:
        connection.execute(
            "INSERT OR IGNORE INTO edinet_source_archives "
            "(doc_id, source_type, stored_on, zip_content) VALUES (?, ?, ?, ?)",
            (doc_id, source_type, stored_on.isoformat(), content),
        )
        result = read_source_archive(connection, doc_id, source_type)
        assert result is not None
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def archive_keys(database: Path) -> set[tuple[str, int]]:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        validate_current_schema(conn)
        return set(conn.execute("SELECT doc_id, source_type FROM edinet_source_archives"))

