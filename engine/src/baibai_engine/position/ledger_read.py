"""資本・取引事実を読む工程へ共通snapshotを産み、壊れた台帳の空表示を止める。

connectionとtransactionはcallerが所有する。読取専用consumerとwriterが同じ
再構築・stored-value契約を使い、初期化・commit・rollbackはここで行わない。
"""

from __future__ import annotations

import json
import sqlite3

from baibai_engine.position.ledger import PortfolioLedgerDocument


class LedgerConflictError(ValueError):
    """The stored ledger or requested replacement conflicts with canonical state."""


class LedgerSchemaError(RuntimeError):
    """The canonical ledger tables are incomplete."""


def _require_schema(connection: sqlite3.Connection) -> bool:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not tables and connection.execute("PRAGMA user_version").fetchone()[0] == 0:
        return False
    if not {"ledger_event", "ledger_market_price", "ledger_meta"} <= tables:
        raise LedgerSchemaError("application DB ledger schema is incomplete")
    return True


def ledger_append_head(connection: sqlite3.Connection) -> int:
    if not _require_schema(connection):
        raise LedgerConflictError("ledger has not been imported")
    return int(
        connection.execute("SELECT coalesce(max(append_seq), 0) FROM ledger_event").fetchone()[0]
    )


def load_ledger_document(connection: sqlite3.Connection) -> PortfolioLedgerDocument | None:
    """Read one canonical replay document; only an unimported ledger is absent."""
    if not _require_schema(connection):
        return None
    meta = connection.execute("SELECT payload FROM ledger_meta WHERE singleton = 1").fetchone()
    if meta is None:
        orphaned = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM ledger_event) OR EXISTS(SELECT 1 FROM ledger_market_price)"
        ).fetchone()[0]
        if orphaned:
            raise LedgerConflictError("ledger rows exist without metadata")
        return None
    raw = json.loads(str(meta[0]))
    if not isinstance(raw, dict):
        raise LedgerConflictError("ledger metadata payload must be an object")
    raw["events"] = [
        json.loads(str(row[0]))
        for row in connection.execute(
            """
            SELECT payload FROM ledger_event
            ORDER BY occurred_at ASC, same_instant_order ASC, append_seq ASC
            """
        )
    ]
    raw["market_prices"] = [
        json.loads(str(row[0]))
        for row in connection.execute("SELECT payload FROM ledger_market_price ORDER BY ticker ASC")
    ]
    document = PortfolioLedgerDocument.model_validate(raw)
    require_canonical_prices(document)
    return document


def require_canonical_prices(document: PortfolioLedgerDocument) -> None:
    if any(price.source_kind == "test_fixture" for price in document.market_prices):
        raise LedgerConflictError("canonical application DB cannot use test_fixture prices")


def load_ledger_in_transaction(
    connection: sqlite3.Connection,
) -> tuple[PortfolioLedgerDocument, int]:
    """Read a required document and its physical append head in the caller's transaction."""
    document = load_ledger_document(connection)
    if document is None:
        raise LedgerConflictError("ledger has not been imported")
    return document, ledger_append_head(connection)
