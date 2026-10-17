"""Save and read received EDINET document-list bodies without interpreting them."""

from __future__ import annotations

import gzip
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta


@dataclass(frozen=True, slots=True)
class EdinetListReceipt:
    receipt_id: str
    doc_date: date
    received_at: datetime
    body: bytes


def retain_list_response(
    conn: sqlite3.Connection, *, doc_date: date, received_at: datetime, body: bytes
) -> str:
    """Commit one raw response on a dedicated connection before any decoding."""
    if conn.in_transaction:
        raise ValueError("EDINET receipt requires a connection without a pending transaction")
    if received_at.utcoffset() != timedelta(0):
        raise ValueError("EDINET receipt time must be UTC")
    receipt_id = uuid.uuid4().hex
    try:
        conn.execute(
            "INSERT INTO edinet_list_responses("
            "receipt_id, doc_date, received_at_utc, body_gzip"
            ") VALUES (?, ?, ?, ?)",
            (
                receipt_id,
                doc_date.isoformat(),
                received_at.astimezone(UTC).isoformat(timespec="microseconds"),
                gzip.compress(body, mtime=0),
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return receipt_id


def read_list_response(conn: sqlite3.Connection, receipt_id: str) -> EdinetListReceipt | None:
    row = conn.execute(
        "SELECT doc_date, received_at_utc, body_gzip "
        "FROM edinet_list_responses WHERE receipt_id = ?",
        (receipt_id,),
    ).fetchone()
    if row is None:
        return None
    return EdinetListReceipt(
        receipt_id=receipt_id,
        doc_date=date.fromisoformat(row[0]),
        received_at=datetime.fromisoformat(row[1]),
        body=gzip.decompress(row[2]),
    )
