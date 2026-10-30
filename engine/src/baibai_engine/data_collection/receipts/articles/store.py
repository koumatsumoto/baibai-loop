"""Append-only L1 writes and exact local reads, within caller transactions."""

from __future__ import annotations

import gzip
import hashlib
import sqlite3

from baibai_engine.data_collection.receipts.articles.contracts import (
    ArticleReceipt,
    BodyRef,
    utc_day,
)


class ArticleStoreError(ValueError):
    """Missing closure, mismatched immutable identity, or invalid received envelope."""


def retain_receipt(
    conn: sqlite3.Connection, receipt: ArticleReceipt, *, body: bytes | None = None
) -> None:
    ref = receipt.body_ref
    if receipt.status == "ok":
        if receipt.http_status != 200 or body is None or ref is None:
            raise ArticleStoreError("200 receipt requires its original body")
        if ref.receipt_id != receipt.receipt_id or hashlib.sha256(body).hexdigest() != ref.sha256:
            raise ArticleStoreError("200 body identity mismatch")
    elif receipt.status == "not_modified":
        if receipt.http_status != 304 or body is not None or ref is None:
            raise ArticleStoreError("304 requires an exact prior 200 reference and no body")
        baseline = read_receipt(conn, ref.receipt_id)
        if (
            baseline.status != "ok"
            or baseline.body_ref != ref
            or baseline.received_at > receipt.received_at
            or (baseline.source, baseline.requested_url, baseline.representation)
            != (receipt.source, receipt.requested_url, receipt.representation)
        ):
            raise ArticleStoreError("304 baseline is not the same request/representation")
        read_body(conn, ref)
    elif body is not None or ref is not None:
        raise ArticleStoreError("unsuccessful receipt cannot supply a successful body reference")
    encoded = gzip.compress(body, mtime=0) if body is not None else None
    row = (
        receipt.receipt_id,
        receipt.source,
        utc_day(receipt.received_at),
        receipt.sha256,
        receipt.model_dump_json(),
        encoded,
    )
    previous = conn.execute(
        "SELECT receipt_id,source,received_on,receipt_sha256,receipt_json,body_gzip "
        "FROM article_source_receipts WHERE receipt_id=?",
        (receipt.receipt_id,),
    ).fetchone()
    if previous is not None:
        if tuple(previous) != row:
            raise ArticleStoreError("receipt ID already carries different content")
        return
    conn.execute("INSERT INTO article_source_receipts VALUES (?,?,?,?,?,?)", row)


def read_receipt(
    conn: sqlite3.Connection, receipt_id: str, *, receipt_sha256: str | None = None
) -> ArticleReceipt:
    row = conn.execute(
        "SELECT receipt_sha256,receipt_json FROM article_source_receipts WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone()
    if row is None:
        raise ArticleStoreError("article receipt is not retained")
    receipt = ArticleReceipt.model_validate_json(row[1])
    if receipt.receipt_id != receipt_id or receipt.sha256 != row[0]:
        raise ArticleStoreError("stored article receipt identity mismatch")
    if receipt_sha256 is not None and receipt.sha256 != receipt_sha256:
        raise ArticleStoreError("article receipt differs from exact reference")
    return receipt


def read_body(conn: sqlite3.Connection, ref: BodyRef) -> bytes:
    receipt = read_receipt(conn, ref.receipt_id)
    if receipt.status != "ok" or receipt.body_ref != ref:
        raise ArticleStoreError("body reference must name its original 200 receipt")
    row = conn.execute(
        "SELECT body_gzip FROM article_source_receipts WHERE receipt_id=?", (ref.receipt_id,)
    ).fetchone()
    if row is None or row[0] is None:
        raise ArticleStoreError("original article body is missing")
    body = gzip.decompress(row[0])
    if hashlib.sha256(body).hexdigest() != ref.sha256:
        raise ArticleStoreError("article body digest mismatch")
    return body


def copy_receipt_closure(
    source: sqlite3.Connection, target: sqlite3.Connection, receipt_id: str
) -> None:
    """Copy only the requested receipt and its concrete 200 (e.g. immutable stage).

    Stage manifest validation is the Jobs caller's responsibility. No clock/ID is
    changed, and replay never selects or rolls back a current article version.
    """
    receipt = read_receipt(source, receipt_id)
    if receipt.status == "not_modified" and receipt.body_ref is not None:
        copy_receipt_closure(source, target, receipt.body_ref.receipt_id)
    body = (
        read_body(source, receipt.body_ref) if receipt.status == "ok" and receipt.body_ref else None
    )
    retain_receipt(target, receipt, body=body)
