"""Replay only TradingView receipts onto a fresh market store, preserving saved rows."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.jquants.receipts import read_complete_request
from baibai_engine.data_collection.tradingview.collector import (
    calendar_context,
    preflight_snapshot,
    universe,
    validate_time,
)
from baibai_engine.data_collection.tradingview.contract import BATCH_TOOL
from baibai_engine.data_collection.tradingview.diagnostics import (
    SourceDataError,
    failure_category,
)
from baibai_engine.data_collection.tradingview.receipts import read_tool_results
from baibai_engine.machine_analysis.models.external_assessment.tradingview.contract import COLUMNS, TABLE
from baibai_engine.machine_analysis.models.external_assessment.tradingview.decode import decode_tool_result
from baibai_engine.machine_analysis.models.external_assessment.tradingview.observations import (
    COLUMNS as REQUEST_COLUMNS,
)
from baibai_engine.machine_analysis.models.external_assessment.tradingview.observations import (
    FetchError,
    normalize_batch,
)
from baibai_engine.market.jquants_projection import project_request
from baibai_engine.storage.observations.schema import open_connection, validate_current_schema

_RAW_KEYS = {
    "tradingview_source_receipts": ("receipt_id",),
    "jquants_source_pages": ("request_id", "page_index"),
}


@dataclass(frozen=True, slots=True)
class TradingViewHandoff:
    snapshot_date: date
    master_asof: date
    symbols: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    master_request_ids: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "snapshot_date": self.snapshot_date.isoformat(),
            "master_asof": self.master_asof.isoformat(),
            "symbols": list(self.symbols),
            "receipt_ids": list(self.receipt_ids),
            "master_request_ids": list(self.master_request_ids),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> TradingViewHandoff:
        return cls(
            snapshot_date=date.fromisoformat(value["snapshot_date"]),
            master_asof=date.fromisoformat(value["master_asof"]),
            symbols=tuple(value["symbols"]),
            receipt_ids=tuple(value["receipt_ids"]),
            master_request_ids=tuple(value["master_request_ids"]),
        )


def export_handoff(source: Path, output: Path, *, day: date, since: datetime) -> TradingViewHandoff:
    """Retain new MCP receipts and any exact-date master received in this acquisition.

    The output has no L2 facts, source coverage, or origin from the working copy.
    The publisher must hydrate fresh canonical state and replay these receipts.
    """
    if output.exists():
        raise FileExistsError(output)
    if since.tzinfo is None:
        raise ValueError("TradingView handoff cutoff requires a timezone")
    cutoff = since.astimezone(UTC).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        validate_current_schema(conn)
        conn.execute("BEGIN")
        basis, _ = calendar_context(conn, day)
        symbols = tuple(universe(conn, basis))
        receipts = tuple(
            row[0]
            for row in conn.execute(
                "SELECT receipt_id FROM tradingview_source_receipts "
                "WHERE snapshot_date = ? AND received_at_utc >= ? "
                "ORDER BY received_at_utc, receipt_id",
                (day.isoformat(), cutoff),
            )
        )
        masters = tuple(
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT request_id FROM jquants_source_pages "
                "WHERE source = 'master' AND received_at_utc >= ? ORDER BY request_id",
                (cutoff,),
            )
        )
        with closing(open_connection(output)) as target, target:
            for table, column, identities in (
                ("tradingview_source_receipts", "receipt_id", receipts),
                ("jquants_source_pages", "request_id", masters),
            ):
                for identity in identities:
                    # All identifiers are fixed in the receipt-only selection above.
                    rows = conn.execute(f"SELECT * FROM {table} WHERE {column} = ?", (identity,))  # nosec B608
                    placeholders = ",".join("?" for _ in rows.description or ())
                    target.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)  # nosec B608
    return TradingViewHandoff(day, basis, symbols, receipts, masters)


def _copy_raw(
    conn: sqlite3.Connection, source: sqlite3.Connection, table: str, identities: Sequence[str]
) -> int:
    keys = _RAW_KEYS[table]
    inserted = 0
    for identity in identities:
        rows = source.execute(f"SELECT * FROM {table} WHERE {keys[0]} = ?", (identity,))  # nosec B608
        names = tuple(item[0] for item in rows.description or ())
        positions = tuple(names.index(key) for key in keys)
        where = " AND ".join(f"{key} = ?" for key in keys)
        for row in rows:
            existing = conn.execute(
                f"SELECT * FROM {table} WHERE {where}",  # nosec B608
                tuple(row[index] for index in positions),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != tuple(row):
                    raise SourceDataError("TradingView handoff receipt identity conflicts")
                continue
            placeholders = ",".join("?" for _ in names)
            conn.execute(f"INSERT INTO {table} VALUES ({placeholders})", row)  # nosec B608
            inserted += 1
    return inserted


def import_handoff(source: Path, target: Path, handoff: TradingViewHandoff) -> dict[str, object]:
    """Add original receipts and replay missing tickers; never replace a saved snapshot.

    A changed calendar/master universe refuses the handoff. A master received by
    the acquisition is projected only when canonical state still lacks that date.
    A canonical master already present is retained, including its newer fields.
    """
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as raw,
        closing(open_connection(target)) as conn,
    ):
        validate_current_schema(raw)
        basis, _ = calendar_context(conn, handoff.snapshot_date)
        if basis != handoff.master_asof:
            raise SourceDataError("TradingView handoff differs from canonical calendar")
        has_master = (
            conn.execute(
                "SELECT 1 FROM jquants_master_snapshots WHERE snapshot_date = ? LIMIT 1",
                (basis.isoformat(),),
            ).fetchone()
            is not None
        )
        if has_master and tuple(universe(conn, basis)) != handoff.symbols:
            raise SourceDataError("TradingView handoff universe differs from canonical master")
        if not has_master and len(handoff.master_request_ids) != 1:
            raise SourceDataError("TradingView handoff requires one complete exact-date master")
        for request_id in handoff.master_request_ids:
            request = read_complete_request(raw, request_id)
            if request.source != "master" or dict(request.params) != {"date": basis.isoformat()}:
                raise SourceDataError("TradingView handoff has a different master request")
        with conn:
            _copy_raw(conn, raw, "jquants_source_pages", handoff.master_request_ids)
            raw_added = _copy_raw(conn, raw, "tradingview_source_receipts", handoff.receipt_ids)
    if not has_master:
        project_request(target, handoff.master_request_ids[0])
    before = preflight_snapshot(target, handoff.snapshot_date)
    with closing(open_connection(target)) as conn, conn:
        if tuple(universe(conn, basis)) != handoff.symbols:
            raise SourceDataError("TradingView handoff universe differs from replayed master")
        basis, business = calendar_context(conn, handoff.snapshot_date)
        names = tuple(name for name, _, _, _ in COLUMNS)
        sql = (
            f"INSERT OR IGNORE INTO {TABLE} ({','.join(names)}) "  # nosec B608
            f"VALUES ({','.join('?' for _ in names)})"
        )
        added = 0
        rejected = []
        for receipt in read_tool_results(conn, handoff.receipt_ids):
            if (
                receipt.snapshot_date != handoff.snapshot_date
                or receipt.master_asof != basis
                or receipt.tool_name != BATCH_TOOL
                or receipt.requested_columns != tuple(REQUEST_COLUMNS)
                or not 1 <= len(receipt.requested_symbols) <= 50
                or len(set(receipt.requested_symbols)) != len(receipt.requested_symbols)
                or not set(receipt.requested_symbols) <= set(handoff.symbols)
            ):
                raise SourceDataError(
                    "TradingView handoff receipt has a different observation context"
                )
            try:
                validate_time(
                    handoff.snapshot_date, receipt.received_at_utc, is_business_day=business
                )
                rows = normalize_batch(
                    decode_tool_result(receipt.result_json),
                    list(receipt.requested_symbols),
                    receipt.received_at_utc,
                )
            except FetchError as error:
                category = failure_category(error)
                if category in {"storage", "internal"}:
                    raise
                rejected.append({"receipt_id": receipt.receipt_id, "reason": category})
                continue
            changes = conn.total_changes
            conn.executemany(
                sql,
                [
                    tuple(
                        handoff.snapshot_date.isoformat()
                        if name == "snapshot_date"
                        else row.get(name)
                        for name in names
                    )
                    for row in rows
                ],
            )
            added += conn.total_changes - changes
    after = preflight_snapshot(target, handoff.snapshot_date)
    return {
        **after,
        "rows_added": added,
        "raw_receipts_added": raw_added,
        "rows_before": before.get("rows", 0),
        "rejected_receipts": rejected,
    }
