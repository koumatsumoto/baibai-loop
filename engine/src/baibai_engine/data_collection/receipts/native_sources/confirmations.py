"""Latest native-record confirmation, separate from change-only source content.

A confirmation points to the exact adopted change receipt. It is acquisition
state, not a new source payload or a claim of historical publication availability.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

from baibai_engine.data_collection.receipts.native_sources.receipt_policy import retain_unchanged
from baibai_engine.data_collection.receipts.native_sources.records import (
    NativeRecords,
    store_snapshot,
)

TABLE = "economic_source_record_confirmations"
COLUMNS = ("source_id", "observed_at", "checked_at", "adopted_fetched_at", "content_sha256")


def confirm_snapshot(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    observed_at: date,
    fetched_at: datetime,
    records: Sequence[Mapping[str, object]],
) -> bool:
    """Record an actual successful response, including an unchanged confirmation."""
    # Unlike legacy scoped seeds, this path carries the actual recheck clock and
    # its content reference. The handoff can reconstruct A→B→A without copying A.
    has_confirmation = (
        connection.execute(
            "SELECT 1 FROM economic_source_record_confirmations "
            "WHERE source_id=? AND observed_at=?",
            (source_id, observed_at.isoformat()),
        ).fetchone()
        is not None
    )
    # A legacy [] may only be a calendar filler. Today's complete response can
    # establish disappearance, but cannot give that old [] a provider meaning.
    token = retain_unchanged.set(not records and not has_confirmation)
    try:
        changed = store_snapshot(
            connection,
            source_id=source_id,
            observed_at=observed_at,
            fetched_at=fetched_at,
            records=records,
        )
    finally:
        retain_unchanged.reset(token)
    key = (source_id, observed_at.isoformat())
    checked_at = fetched_at.astimezone(UTC).isoformat(timespec="microseconds")
    adopted = connection.execute(
        "SELECT fetched_at, payload FROM economic_source_records "
        "WHERE source_id=? AND observed_at=? AND fetched_at=?",
        (*key, checked_at),
    ).fetchone()
    if adopted is None:
        adopted = connection.execute(
            "SELECT fetched_at, payload FROM economic_source_records "
            "WHERE source_id=? AND observed_at=? AND fetched_at=COALESCE("
            "(SELECT adopted_fetched_at FROM economic_source_record_confirmations "
            "WHERE source_id=? AND observed_at=?),"
            "(SELECT MAX(fetched_at) FROM economic_source_records "
            "WHERE source_id=? AND observed_at=?))",
            (*key, *key, *key),
        ).fetchone()
    assert adopted is not None
    if str(adopted[0]) > checked_at:
        raise ValueError("native confirmation predates the latest retained source receipt")
    save_confirmation(
        connection,
        (*key, checked_at, str(adopted[0]), hashlib.sha256(adopted[1]).hexdigest()),
    )
    return changed


def save_confirmation(connection: sqlite3.Connection, row: tuple[str, ...]) -> None:
    """Merge one latest confirmation without rolling its clock or anchor backward."""
    previous = connection.execute(
        "SELECT * FROM economic_source_record_confirmations WHERE source_id=? AND observed_at=?",
        row[:2],
    ).fetchone()
    if previous is not None:
        if row[2] < previous[2]:
            if row[2] > previous[3] and row[4] != previous[4]:
                raise ValueError("native confirmation requires fresh-canonical handoff acceptance")
            return
        if row[2] == previous[2] and tuple(row) != tuple(previous):
            raise ValueError("conflicting native confirmation at the same received time")
        if row[3] < previous[3]:
            raise ValueError("native confirmation requires fresh-canonical handoff acceptance")
    payload = connection.execute(
        "SELECT payload FROM economic_source_records "
        "WHERE source_id=? AND observed_at=? AND fetched_at=?",
        (*row[:2], row[3]),
    ).fetchone()
    if row[2] < row[3] or payload is None or hashlib.sha256(payload[0]).hexdigest() != row[4]:
        raise ValueError("native confirmation does not match its adopted source receipt")
    connection.execute(
        "INSERT INTO economic_source_record_confirmations VALUES (?,?,?,?,?) "
        "ON CONFLICT(source_id,observed_at) DO UPDATE SET checked_at=excluded.checked_at, "
        "adopted_fetched_at=excluded.adopted_fetched_at,content_sha256=excluded.content_sha256",
        row,
    )


def collection_digest(connection: sqlite3.Connection, source_id: str, checked_at: str) -> str:
    """Validate a complete native response using its self-contained period anchors."""
    if connection.execute(
        "SELECT observed_at FROM economic_source_records WHERE source_id=? AND record_count>0 "
        "EXCEPT SELECT observed_at FROM economic_source_record_confirmations WHERE source_id=?",
        (source_id, source_id),
    ).fetchone():
        raise ValueError("complete source confirmation is missing a known native period")
    periods = []
    for day, checked, adopted, digest in connection.execute(
        "SELECT observed_at,checked_at,adopted_fetched_at,content_sha256 "
        "FROM economic_source_record_confirmations WHERE source_id=? ORDER BY observed_at",
        (source_id,),
    ):
        anchor = connection.execute(
            "SELECT record_count,payload FROM economic_source_records "
            "WHERE source_id=? AND observed_at=? AND fetched_at=?",
            (source_id, day, adopted),
        ).fetchone()
        if (
            checked != checked_at
            or adopted > checked
            or anchor is None
            or hashlib.sha256(anchor[1]).hexdigest() != digest
        ):
            raise ValueError("complete source confirmation lost its period anchor")
        if anchor[0] > 0:
            periods.append((day, digest))
    if not periods:
        raise ValueError("complete FRED response has no native periods")
    return hashlib.sha256(json.dumps(periods, separators=(",", ":")).encode()).hexdigest()


def save_collection_confirmation(connection: sqlite3.Connection, row: tuple[str, ...]) -> None:
    previous = connection.execute(
        "SELECT * FROM economic_source_collection_confirmations WHERE source_id=?", row[:1]
    ).fetchone()
    if previous is not None:
        if row[1] < previous[1]:
            return
        if row[1] == previous[1] and tuple(row) != tuple(previous):
            raise ValueError("conflicting complete source confirmation at the same received time")
    if collection_digest(connection, row[0], row[1]) != row[2]:
        raise ValueError("complete source confirmation differs from its period set")
    connection.execute(
        "INSERT INTO economic_source_collection_confirmations VALUES (?,?,?) "
        "ON CONFLICT(source_id) DO UPDATE SET checked_at=excluded.checked_at, "
        "content_sha256=excluded.content_sha256",
        row,
    )


def confirm_source_response(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    fetched_at: datetime,
    records_by_day: NativeRecords,
) -> None:
    """Confirm one actual complete FRED response, never a range/run reconstruction."""
    if not records_by_day or any(not rows for rows in records_by_day.values()):
        raise ValueError("complete FRED response requires its native nonempty periods")
    periods = set(records_by_day) | {
        date.fromisoformat(row[0])
        for row in connection.execute(
            "SELECT DISTINCT observed_at FROM economic_source_records "
            "WHERE source_id=? AND record_count>0",
            (source_id,),
        )
    }
    for day in sorted(periods):
        confirm_snapshot(
            connection,
            source_id=source_id,
            observed_at=day,
            fetched_at=fetched_at,
            records=records_by_day.get(day, ()),
        )
    checked = fetched_at.astimezone(UTC).isoformat(timespec="microseconds")
    save_collection_confirmation(
        connection, (source_id, checked, collection_digest(connection, source_id, checked))
    )


def validate_collection_confirmations(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT DISTINCT source_id FROM economic_source_record_confirmations "
        "EXCEPT SELECT source_id FROM economic_source_collection_confirmations"
    ).fetchone():
        raise ValueError("period confirmation requires an explicit complete source recheck")
    for source_id, checked, digest in connection.execute(
        "SELECT * FROM economic_source_collection_confirmations"
    ):
        if collection_digest(connection, source_id, checked) != digest:
            raise ValueError("complete source confirmation differs from its period set")
