"""Native daily source records, independent of their L2 indicator uses.

The source date is PubDate for investor flows and the observation date for other
providers. Economic aggregation periods remain inside the unmodified records.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    observed_at: date
    fetched_at: datetime
    payload: bytes

    @property
    def records(self) -> tuple[Mapping[str, object], ...]:
        return decode_records(self.payload)


def decode_records(payload: bytes) -> tuple[Mapping[str, object], ...]:
    records = json.loads(gzip.decompress(payload))
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError("stored source snapshot must contain native records")
    return tuple(records)


def store_snapshot(
    connection: sqlite3.Connection,
    *,
    source_key: str,
    observed_at: date,
    fetched_at: datetime,
    records: Sequence[Mapping[str, object]],
) -> bool:
    """Stage a changed complete day; the acquisition coordinator owns its commit.

    The payload is native provider-record JSON, not the original HTTP bytes.
    Empty records are an observed empty response, not a failed or unexamined day.
    """
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("source acquisition time must include a timezone")
    payload = gzip.compress(
        json.dumps(records, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8"),
        mtime=0,
    )
    latest = connection.execute(
        "SELECT payload FROM indicator_source_snapshots WHERE source_key = ? AND observed_at = ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (source_key, observed_at.isoformat()),
    ).fetchone()
    if latest is not None and latest[0] == payload:
        return False
    connection.execute(
        "INSERT INTO indicator_source_snapshots"
        "(source_key, observed_at, fetched_at, record_count, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            source_key,
            observed_at.isoformat(),
            fetched_at.astimezone(UTC).isoformat(timespec="microseconds"),
            len(records),
            payload,
        ),
    )
    return True


def stored_days(
    connection: sqlite3.Connection, start: date, end: date, *, source_key: str
) -> set[date]:
    """Completed source days, including empty responses, with no calculation claim."""
    return {
        date.fromisoformat(row[0])
        for row in connection.execute(
            "SELECT DISTINCT observed_at FROM indicator_source_snapshots "
            "WHERE source_key = ? AND observed_at BETWEEN ? AND ?",
            (source_key, start.isoformat(), end.isoformat()),
        )
    }


def read_snapshots(
    connection: sqlite3.Connection, start: date, end: date, *, source_key: str
) -> Iterator[SourceSnapshot]:
    """Read each day's latest acquired payload, without restoring older nonempty rows.

    Acquisition time orders revisions; it does not establish historical publication
    availability. Decode one day at a time rather than retaining a full history's JSON.
    """
    rows = connection.execute(
        """
        SELECT c.observed_at, c.fetched_at, c.payload
        FROM indicator_source_snapshots c
        WHERE c.source_key = ? AND c.observed_at BETWEEN ? AND ?
          AND c.fetched_at = (
              SELECT MAX(newest.fetched_at) FROM indicator_source_snapshots newest
              WHERE newest.source_key = c.source_key AND newest.observed_at = c.observed_at
          )
        ORDER BY c.observed_at
        """,
        (source_key, start.isoformat(), end.isoformat()),
    )
    for observed_at, fetched_at, payload in rows:
        yield SourceSnapshot(
            date.fromisoformat(observed_at),
            datetime.fromisoformat(fetched_at),
            payload,
        )
