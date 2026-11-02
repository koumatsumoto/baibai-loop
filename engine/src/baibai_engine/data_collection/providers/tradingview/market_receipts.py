"""既存native-document storeへMCP応答を保持する。schema追加や日次TV充足変更は不要。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from mcp.types import CallToolResult

from baibai_engine.data_collection.providers.tradingview.market_models import (
    MARKET_SOURCE_ID,
    MarketToolCapture,
)
from baibai_engine.data_collection.receipts.native_sources.documents import (
    SourceDocument,
    store_document,
)

SOURCE_URL = "https://mcp.tradingview.com/mcp"


def retain_market_capture(conn: sqlite3.Connection, capture: MarketToolCapture) -> bool:
    """Commit one reception; a reused receipt ID must describe the exact same reception."""
    if conn.in_transaction:
        raise ValueError("market receipt requires no pending transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        prior = conn.execute(
            "SELECT fetched_at FROM economic_source_documents WHERE source_id=? AND request_key=?",
            (MARKET_SOURCE_ID, capture.receipt_id),
        ).fetchall()
        received = capture.received_at_utc.astimezone(UTC).isoformat(timespec="microseconds")
        if prior and (len(prior) != 1 or prior[0][0] != received):
            raise ValueError("market receipt ID conflicts with an earlier reception")
        inserted = store_document(
            conn,
            SourceDocument(
                source_id=MARKET_SOURCE_ID,
                request_key=capture.receipt_id,
                fetched_at=capture.received_at_utc,
                source_url=SOURCE_URL,
                content_type="application/json",
                payload=capture.result_bytes(),
                metadata=capture.metadata(),
            ),
        )
        conn.commit()
        return inserted
    except BaseException:
        conn.rollback()
        raise


def read_market_capture(
    conn: sqlite3.Connection, receipt_id: str, *, expected_sha256: str
) -> MarketToolCapture:
    rows = conn.execute(
        "SELECT fetched_at,source_url,content_type,payload,metadata "
        "FROM economic_source_documents WHERE source_id=? AND request_key=?",
        (MARKET_SOURCE_ID, receipt_id),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("exact market receipt is missing or ambiguous")
    fetched_at, url, content_type, payload, metadata_json = rows[0]
    metadata = json.loads(metadata_json)
    if (
        metadata.pop("format", None) != "tradingview-market-receipt-v1"
        or url != SOURCE_URL
        or content_type != "application/json"
        or metadata.get("receipt_id") != receipt_id
    ):
        raise ValueError("market receipt source contract changed")
    capture = MarketToolCapture(
        **metadata,
        received_at_utc=datetime.fromisoformat(fetched_at),
        result=CallToolResult.model_validate_json(payload),
    )
    if capture.digest() != expected_sha256:
        raise ValueError("exact market receipt digest differs")
    return capture
