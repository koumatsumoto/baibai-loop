"""Rebuild one TradingView observation date from explicitly selected L1 receipts."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.data_collection.tradingview.collector import (
    SourceDataError,
    calendar_context,
    universe,
    validate_time,
)
from baibai_engine.data_collection.tradingview.contract import BATCH_TOOL
from baibai_engine.data_collection.tradingview.receipts import read_tool_results
from baibai_engine.machine_analysis.external_assessment.tradingview.contract import COLUMNS, TABLE
from baibai_engine.machine_analysis.external_assessment.tradingview.decode import decode_tool_result
from baibai_engine.data_collection.tradingview.diagnostics import (
    failure_category,
)
from baibai_engine.machine_analysis.external_assessment.tradingview.observations import (
    COLUMNS as REQUEST_COLUMNS,
)
from baibai_engine.machine_analysis.external_assessment.tradingview.observations import (
    FetchError,
    normalize_batch,
)
from baibai_engine.storage.observations.schema import validate_current_schema


class RebuildError(SourceDataError):
    def __init__(self, rejected: tuple[dict[str, str], ...]) -> None:
        self.rejected = rejected
        super().__init__("No valid TradingView receipts were selected")


def rebuild_from_receipts(path: Path, day: date, receipt_ids: Sequence[str]) -> dict[str, object]:
    """Replace only one L2 date after validating the entire explicit receipt selection."""
    if not receipt_ids or len(receipt_ids) != len(set(receipt_ids)):
        raise SourceDataError("TradingView receipt IDs must be nonempty and unique")
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True)) as conn:
        validate_current_schema(conn)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = _replace_selected_day(conn, day, receipt_ids)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


def _replace_selected_day(
    conn: sqlite3.Connection, day: date, receipt_ids: Sequence[str]
) -> dict[str, object]:
    basis, business = calendar_context(conn, day)
    symbols = universe(conn, basis)
    allowed = set(symbols)
    receipts = read_tool_results(conn, receipt_ids)
    columns = tuple(REQUEST_COLUMNS)
    for receipt in receipts:
        if receipt.snapshot_date != day or receipt.master_asof != basis:
            raise SourceDataError("TradingView receipt has a different observation context")
        if receipt.tool_name != BATCH_TOOL or receipt.requested_columns != columns:
            raise SourceDataError("TradingView receipt has an unsupported tool or columns")
        requested = receipt.requested_symbols
        if not 1 <= len(requested) <= 50 or len(requested) != len(set(requested)):
            raise SourceDataError("TradingView receipt has invalid requested symbols")
        if not set(requested) <= allowed:
            raise SourceDataError("TradingView receipt requests a symbol outside the universe")

    names = [name for name, _, _, _ in COLUMNS]
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    rejected: list[dict[str, str]] = []
    valid_receipts = 0
    for receipt in receipts:
        try:
            validate_time(day, receipt.received_at_utc, is_business_day=business)
            payload = decode_tool_result(receipt.result_json)
            decoded = normalize_batch(
                payload, list(receipt.requested_symbols), receipt.received_at_utc
            )
        except FetchError as exc:
            rejected.append({"receipt_id": receipt.receipt_id, "reason": failure_category(exc)})
            continue
        valid_receipts += 1
        tickers = {str(row["ticker"]) for row in decoded}
        if seen & tickers:
            raise SourceDataError("TradingView receipts overlap for one date and ticker")
        seen.update(tickers)
        rows.extend(decoded)
    if valid_receipts == 0:
        raise RebuildError(tuple(rejected))

    sql = f"INSERT INTO {TABLE} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})"  # nosec B608
    conn.execute(f"DELETE FROM {TABLE} WHERE snapshot_date=?", (day.isoformat(),))  # nosec B608
    conn.executemany(
        sql,
        [
            tuple(day.isoformat() if name == "snapshot_date" else row.get(name) for name in names)
            for row in rows
        ],
    )
    return {
        "snapshot_date": day.isoformat(),
        "master_asof": basis.isoformat(),
        "rows": len(rows),
        "unresolved": sum(row["fetch_status"] == "unresolved" for row in rows),
        "expected_universe": len(symbols),
        "remaining": len(symbols) - len(rows),
        "rejected_receipts": rejected,
    }
