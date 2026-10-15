"""Native daily option-chain facts, independent of their L2 indicator uses."""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime


@dataclass(frozen=True, slots=True)
class OptionChain:
    trade_date: date
    fetched_at: datetime
    payload: bytes

    @property
    def records(self) -> tuple[Mapping[str, object], ...]:
        records = json.loads(gzip.decompress(self.payload))
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError("stored option chain must contain native records")
        return tuple(records)


def store_chain(
    connection: sqlite3.Connection,
    *,
    trade_date: date,
    fetched_at: datetime,
    records: Sequence[Mapping[str, object]],
) -> bool:
    """Stage a changed complete day; the acquisition coordinator owns its commit.

    The payload is native provider-record JSON, not the original HTTP bytes.
    Empty records are an observed empty response, not a failed or unexamined day.
    """
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("option acquisition time must include a timezone")
    payload = gzip.compress(
        json.dumps(records, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8"),
        mtime=0,
    )
    latest = connection.execute(
        "SELECT payload FROM jquants_option_chains WHERE trade_date = ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (trade_date.isoformat(),),
    ).fetchone()
    if latest is not None and latest[0] == payload:
        return False
    connection.execute(
        "INSERT INTO jquants_option_chains(trade_date, fetched_at, record_count, payload) "
        "VALUES (?, ?, ?, ?)",
        (
            trade_date.isoformat(),
            fetched_at.astimezone(UTC).isoformat(timespec="microseconds"),
            len(records),
            payload,
        ),
    )
    return True


def stored_days(connection: sqlite3.Connection, start: date, end: date) -> set[date]:
    """Completed source days, including empty responses, with no calculation claim."""
    return {
        date.fromisoformat(row[0])
        for row in connection.execute(
            "SELECT DISTINCT trade_date FROM jquants_option_chains "
            "WHERE trade_date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        )
    }


def read_chains(
    connection: sqlite3.Connection, start: date, end: date
) -> Iterator[OptionChain]:
    """Read each day's latest acquired payload, without restoring older nonempty rows.

    Acquisition time orders revisions; it does not establish historical publication
    availability. Decode one day at a time rather than retaining a full history's JSON.
    """
    rows = connection.execute(
        """
        SELECT c.trade_date, c.fetched_at, c.payload
        FROM jquants_option_chains c
        WHERE c.trade_date BETWEEN ? AND ?
          AND c.fetched_at = (
              SELECT MAX(newest.fetched_at) FROM jquants_option_chains newest
              WHERE newest.trade_date = c.trade_date
          )
        ORDER BY c.trade_date
        """,
        (start.isoformat(), end.isoformat()),
    )
    for trade_date, fetched_at, payload in rows:
        yield OptionChain(
            date.fromisoformat(trade_date),
            datetime.fromisoformat(fetched_at),
            payload,
        )
