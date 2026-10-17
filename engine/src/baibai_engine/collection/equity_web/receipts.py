"""Retain received public HTML bytes before any interpretation."""

from __future__ import annotations

import gzip
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

from baibai_engine.collection.equity_web.models import EquityWebSourceResponse, Role, Source
from baibai_engine.collection.identifiers import SecurityCodeError, normalize_ticker


class EquityWebReceiptError(ValueError):
    """A requested source response cannot be retained or read."""


def retain_response(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    source: Source,
    requested_ticker: str,
    role: Role,
    parent_receipt_id: str | None,
    requested_url: str,
    received_at_utc: datetime,
    selected_encoding: str,
    body: bytes,
) -> str:
    """Commit one original response independently on an idle caller-owned connection."""
    _uuid_text(attempt_id, "attempt_id")
    if source not in ("monex", "rakuten", "kabumap"):
        raise EquityWebReceiptError("unsupported equity web source")
    if role not in ("page", "iframe"):
        raise EquityWebReceiptError("unsupported equity web response role")
    try:
        if normalize_ticker(requested_ticker) != requested_ticker:
            raise EquityWebReceiptError("requested ticker must be canonical")
    except (SecurityCodeError, AttributeError, TypeError) as exc:
        raise EquityWebReceiptError("requested ticker must be canonical") from exc
    _nonempty(requested_url, "requested_url")
    _nonempty(selected_encoding, "selected_encoding")
    if not isinstance(cast(object, received_at_utc), datetime) or (
        received_at_utc.utcoffset() != timedelta(0)
    ):
        raise EquityWebReceiptError("received_at_utc must be UTC aware")
    if not isinstance(cast(object, body), bytes):
        raise EquityWebReceiptError("response body must be bytes")
    if role == "page":
        if parent_receipt_id is not None:
            raise EquityWebReceiptError("page response cannot have a parent")
    else:
        if source != "rakuten" or parent_receipt_id is None:
            raise EquityWebReceiptError("only a Rakuten iframe can have a page parent")
        _uuid_text(parent_receipt_id, "parent_receipt_id")
    if conn.in_transaction:
        raise EquityWebReceiptError("equity web receipt requires no pending transaction")
    receipt_id = str(uuid.uuid4())
    conn.execute("BEGIN IMMEDIATE")
    try:
        if parent_receipt_id is not None:
            parent = conn.execute(
                "SELECT attempt_id, source, requested_ticker, role "
                "FROM equity_web_source_responses WHERE receipt_id=?",
                (parent_receipt_id,),
            ).fetchone()
            if parent != (attempt_id, source, requested_ticker, "page"):
                raise EquityWebReceiptError("iframe parent must be the same attempt's page")
        existing = conn.execute(
            "SELECT source, requested_ticker FROM equity_web_source_responses "
            "WHERE attempt_id=? LIMIT 1",
            (attempt_id,),
        ).fetchone()
        if existing is not None and existing != (source, requested_ticker):
            raise EquityWebReceiptError("attempt already belongs to another source or ticker")
        conn.execute(
            "INSERT INTO equity_web_source_responses("
            "receipt_id, attempt_id, source, requested_ticker, role, parent_receipt_id, "
            "requested_url, received_at_utc, received_on, selected_encoding, body_gzip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_id,
                attempt_id,
                source,
                requested_ticker,
                role,
                parent_receipt_id,
                requested_url,
                received_at_utc.astimezone(UTC).isoformat(timespec="microseconds"),
                received_at_utc.date().isoformat(),
                selected_encoding,
                gzip.compress(body, mtime=0),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise EquityWebReceiptError("equity web response could not be retained") from exc
    except BaseException:
        conn.rollback()
        raise
    return receipt_id


def read_responses(
    conn: sqlite3.Connection, receipt_ids: Sequence[str]
) -> tuple[EquityWebSourceResponse, ...]:
    """Read only explicit receipt IDs in caller order, without creating a store."""
    if not isinstance(cast(object, receipt_ids), Sequence) or isinstance(
        cast(object, receipt_ids), (str, bytes)
    ):
        raise EquityWebReceiptError("receipt IDs must be a sequence")
    responses: list[EquityWebSourceResponse] = []
    for receipt_id in receipt_ids:
        _uuid_text(receipt_id, "receipt_id")
        row = conn.execute(
            "SELECT receipt_id, attempt_id, source, requested_ticker, role, "
            "parent_receipt_id, requested_url, received_at_utc, selected_encoding, body_gzip "
            "FROM equity_web_source_responses WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            raise EquityWebReceiptError(f"equity web receipt is not retained: {receipt_id}")
        responses.append(
            EquityWebSourceResponse(
                receipt_id=row[0],
                attempt_id=row[1],
                source=row[2],
                requested_ticker=row[3],
                role=row[4],
                parent_receipt_id=row[5],
                requested_url=row[6],
                received_at_utc=datetime.fromisoformat(row[7]),
                selected_encoding=row[8],
                body=gzip.decompress(row[9]),
            )
        )
    return tuple(responses)


def _uuid_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise EquityWebReceiptError(f"{name} must be a canonical UUID")
    try:
        identity = uuid.UUID(value)
    except ValueError as exc:
        raise EquityWebReceiptError(f"{name} must be a canonical UUID") from exc
    if str(identity) != value or identity.int == 0:
        raise EquityWebReceiptError(f"{name} must be a canonical UUID")
    return value


def _nonempty(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise EquityWebReceiptError(f"{name} must be nonempty text")
    return value
