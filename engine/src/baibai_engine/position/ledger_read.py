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


def stored_ledger_rows(
    connection: sqlite3.Connection,
    *,
    kind: str,
    filters: dict[str, object],
    after: list[str | int | float] | None,
    limit: int,
) -> list[dict[str, object]]:
    """Read append-order facts without replay or quote lookup."""
    from pydantic import TypeAdapter

    from baibai_engine.foundation.sqlite_pages import jst_day_ranges, object_payload, select_page
    from baibai_engine.position.ledger import LedgerEvent, LedgerMetadata, MarketPrice

    if not _require_schema(connection):
        raise FileNotFoundError("ledger unwritten")
    meta = connection.execute("SELECT * FROM ledger_meta WHERE singleton=1").fetchone()
    if meta is None:
        raise FileNotFoundError("ledger unimported")
    if kind == "ledger_meta":
        row = dict(meta)
        row["payload"] = object_payload(row["payload"])
        LedgerMetadata.model_validate(row["payload"], extra="ignore")
        row["append_head"] = ledger_append_head(connection)
        return [row]
    if kind not in {"ledger_event", "ledger_market_price"}:
        raise ValueError("unknown ledger rows")
    rows = select_page(
        connection,
        table=kind,
        order=("append_seq",) if kind == "ledger_event" else ("ticker",),
        equal=filters
        if "from" not in filters and "to" not in filters
        else {k: v for k, v in filters.items() if k not in {"from", "to"}},
        ranges=jst_day_ranges("occurred_at", filters) if kind == "ledger_event" else (),
        after=after,
        limit=limit,
    )
    for row in rows:
        payload = object_payload(row["payload"])
        if kind == "ledger_event":
            TypeAdapter(LedgerEvent).validate_python(payload)
            if payload.get("event_id") != row["event_id"]:
                raise LedgerConflictError("event identity differs")
        else:
            price = MarketPrice.model_validate(payload)
            if price.source_kind == "test_fixture" or price.ticker != row["ticker"]:
                raise LedgerConflictError("price identity/source differs")
        row["payload"] = payload
    return rows
