"""Read received EDINET document-list bodies without interpreting them."""

from __future__ import annotations

import gzip
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class EdinetListReceipt:
    receipt_id: str
    doc_date: date
    received_at: datetime
    body: bytes


def read_list_responses(
    conn: sqlite3.Connection, *, doc_date: date
) -> tuple[EdinetListReceipt, ...]:
    """Read all retained responses for a requested day without interpreting bodies."""
    rows = conn.execute(
        "SELECT receipt_id, doc_date, received_at_utc, body_gzip "
        "FROM edinet_list_responses WHERE doc_date = ? "
        "ORDER BY received_at_utc, receipt_id",
        (doc_date.isoformat(),),
    ).fetchall()
    return tuple(_decode_list_response_row(row) for row in rows)


def _decode_list_response_row(row: tuple[str, str, str, bytes]) -> EdinetListReceipt:
    return EdinetListReceipt(
        receipt_id=str(row[0]),
        doc_date=date.fromisoformat(row[1]),
        received_at=datetime.fromisoformat(row[2]),
        body=gzip.decompress(row[3]),
    )
