"""Commit received MCP tool results before any business validation; read by exact ID."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import cast

from mcp.types import CallToolResult

from baibai_engine.collection.tradingview.models import TradingViewSourceReceipt


class TradingViewReceiptError(ValueError):
    """A tool result cannot be retained or an explicit receipt is unavailable."""


def retain_tool_result(
    conn: sqlite3.Connection,
    *,
    snapshot_date: date,
    master_asof: date,
    tool_name: str,
    received_at_utc: datetime,
    requested_symbols: Sequence[str],
    requested_columns: Sequence[str],
    result: CallToolResult,
) -> str:
    """Commit a received SDK result independently of later normalization."""
    if conn.in_transaction:
        raise TradingViewReceiptError("TradingView receipt requires no pending transaction")
    if not isinstance(cast(object, result), CallToolResult):
        raise TradingViewReceiptError("TradingView receipt requires an SDK CallToolResult")
    if not isinstance(cast(object, tool_name), str) or not tool_name:
        raise TradingViewReceiptError("TradingView tool name must be nonempty")
    if not isinstance(cast(object, received_at_utc), datetime) or (
        received_at_utc.utcoffset() != timedelta(0)
    ):
        raise TradingViewReceiptError("TradingView receipt time must be UTC")
    symbols_json = _names_json(requested_symbols)
    columns_json = _names_json(requested_columns)
    # Python mode leaves unsupported objects and non-finite floats visible to json.dumps.
    # JSON mode silently changes NaN and Infinity to null.
    result_json = json.dumps(
        result.model_dump(mode="python", by_alias=True, exclude_none=False),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    receipt_id = uuid.uuid4().hex
    try:
        conn.execute(
            "INSERT INTO tradingview_source_receipts("
            "receipt_id, snapshot_date, master_asof, tool_name, received_at_utc, "
            "requested_symbols_json, requested_columns_json, result_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_id,
                snapshot_date.isoformat(),
                master_asof.isoformat(),
                tool_name,
                received_at_utc.astimezone(UTC).isoformat(timespec="microseconds"),
                symbols_json,
                columns_json,
                result_json,
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return receipt_id


def read_tool_results(
    conn: sqlite3.Connection, receipt_ids: Sequence[str]
) -> tuple[TradingViewSourceReceipt, ...]:
    """Read only the named receipts, preserving requested order and caller transaction."""
    receipts: list[TradingViewSourceReceipt] = []
    for receipt_id in receipt_ids:
        row = conn.execute(
            "SELECT receipt_id, snapshot_date, master_asof, tool_name, received_at_utc, "
            "requested_symbols_json, requested_columns_json, result_json "
            "FROM tradingview_source_receipts WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            raise TradingViewReceiptError(f"TradingView receipt is not retained: {receipt_id}")
        receipts.append(
            TradingViewSourceReceipt(
                receipt_id=row[0],
                snapshot_date=date.fromisoformat(row[1]),
                master_asof=date.fromisoformat(row[2]),
                tool_name=row[3],
                received_at_utc=datetime.fromisoformat(row[4]),
                requested_symbols=tuple(json.loads(row[5])),
                requested_columns=tuple(json.loads(row[6])),
                result_json=row[7],
            )
        )
    return tuple(receipts)


def _names_json(names: Sequence[str]) -> str:
    if isinstance(cast(object, names), (str, bytes)) or any(
        not isinstance(cast(object, name), str) for name in names
    ):
        raise TradingViewReceiptError("TradingView request names must be a sequence of strings")
    return json.dumps(list(names), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
