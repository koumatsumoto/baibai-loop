"""資本・取引事実を読む工程へ共通snapshotを産み、壊れた台帳の空表示を止める。

connectionとtransactionはcallerが所有する。読取専用consumerとwriterが同じ
再構築・stored-value契約を使い、初期化・commit・rollbackはここで行わない。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from pydantic import TypeAdapter

from baibai_engine.portfolio.ledger.events import (
    ExecutionEvent,
    LedgerEvent,
    PortfolioLedgerDocument,
    ReleaseEvent,
    ReservationEvent,
)
from baibai_engine.primitives.time import JST


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
    if not {"ledger_event", "ledger_meta"} <= tables:
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
        orphaned = connection.execute("SELECT EXISTS(SELECT 1 FROM ledger_event)").fetchone()[0]
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
    return PortfolioLedgerDocument.model_validate(raw)


def load_ledger_in_transaction(
    connection: sqlite3.Connection,
) -> tuple[PortfolioLedgerDocument, int]:
    """Read a required document and its physical append head in the caller's transaction."""
    document = load_ledger_document(connection)
    if document is None:
        raise LedgerConflictError("ledger has not been imported")
    return document, ledger_append_head(connection)


def optional_ledger_head(connection: sqlite3.Connection) -> int | None:
    """未導入と、読取済みの空prefix (0) を区別する。全取引のreplayはしない。"""
    if not _require_schema(connection):
        return None
    if connection.execute("SELECT 1 FROM ledger_meta WHERE singleton=1").fetchone() is None:
        if ledger_append_head(connection):
            raise LedgerConflictError("ledger rows exist without metadata")
        return None
    return ledger_append_head(connection)


def load_ledger_events(
    connection: sqlite3.Connection,
    *,
    append_head: int,
    event_ids: tuple[str, ...] | None = None,
) -> tuple[LedgerEvent, ...]:
    """固定prefixか保存済みID列を読む。現在headとの一致や残高適格性は要求しない。"""
    if append_head < 0 or append_head > ledger_append_head(connection):
        raise LedgerConflictError("ledger snapshot head is outside the recorded prefix")
    adapter: TypeAdapter[LedgerEvent] = TypeAdapter(LedgerEvent)
    if event_ids is None:
        rows = connection.execute(
            "SELECT event_id,payload FROM ledger_event WHERE append_seq<=? "
            "ORDER BY occurred_at,same_instant_order,append_seq",
            (append_head,),
        ).fetchall()
    else:
        rows = []
        for identity in event_ids:
            row = connection.execute(
                "SELECT event_id,payload FROM ledger_event WHERE event_id=? AND append_seq<=?",
                (identity, append_head),
            ).fetchone()
            if row is None:
                raise LedgerConflictError(f"ledger snapshot event is unavailable: {identity}")
            rows.append(row)
    events: list[LedgerEvent] = []
    for row in rows:
        event = adapter.validate_json(str(row[1]))
        if event.event_id != str(row[0]):
            raise LedgerConflictError("ledger event identity differs from payload")
        events.append(event)
    return tuple(events)


def related_decision_events(
    connection: sqlite3.Connection,
    *,
    reference: str,
    ticker: str,
    append_head: int,
    as_of: date,
) -> tuple[LedgerEvent, ...]:
    """明示参照の注文/約定とreservation連鎖だけ。同tickerやrelease単独から広げない。"""
    events = tuple(
        event
        for event in load_ledger_events(connection, append_head=append_head)
        if (event.occurred_on or event.occurred_at.astimezone(JST).date()) <= as_of
    )
    direct = tuple(
        event
        for event in events
        if isinstance(event, (ReservationEvent, ExecutionEvent))
        and event.decision_reference == reference
        and event.ticker == ticker
    )
    reservations = {event.reservation_id for event in direct if event.reservation_id is not None}
    direct_ids = {event.event_id for event in direct}
    return tuple(
        event
        for event in events
        if event.event_id in direct_ids
        or (
            isinstance(event, (ReservationEvent, ExecutionEvent, ReleaseEvent))
            and event.reservation_id in reservations
        )
    )
