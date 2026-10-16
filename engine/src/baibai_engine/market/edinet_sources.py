"""Downloaded EDINET ZIP inputs, retained in the existing market/lake store.

The store date partitions bytes first retained here; it is not a filing date or
proof of provider availability. Existing lake publication owns immutable objects
and their retention. No separate acquisition audit or version database is added.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.storage.observations.read import connect_read_only
from baibai_engine.storage.observations.schema import open_connection


def read_source_archive(path: Path, doc_id: str, source_type: int) -> bytes | None:
    if not path.exists():
        return None
    with closing(connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT zip_content FROM edinet_source_archives WHERE doc_id = ? AND source_type = ?",
            (doc_id, source_type),
        ).fetchone()
    return bytes(row[0]) if row is not None else None


def retain_source_archive(
    path: Path, doc_id: str, source_type: int, content: bytes, *, stored_on: date
) -> bytes:
    """Keep the existing cached source if a concurrent downloader already retained it.

    This is a cache identity, not a claim that the API's ZIP bytes can never change.
    Source refresh and selection must respect the filing's disclosure state.
    """
    with closing(open_connection(path)) as connection, connection:
        connection.execute(
            "INSERT OR IGNORE INTO edinet_source_archives "
            "(doc_id, source_type, stored_on, zip_content) VALUES (?, ?, ?, ?)",
            (doc_id, source_type, stored_on.isoformat(), content),
        )
        row = connection.execute(
            "SELECT zip_content FROM edinet_source_archives WHERE doc_id = ? AND source_type = ?",
            (doc_id, source_type),
        ).fetchone()
    assert row is not None
    return bytes(row[0])
