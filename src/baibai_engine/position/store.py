"""Application-DB storage for the append-only portfolio ledger."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.position.ledger import (
    LedgerEvent,
    MarketPrice,
    PortfolioLedgerDocument,
)


class LedgerConflictError(ValueError):
    """The requested write does not match the current append head or rows."""


class LedgerSchemaError(RuntimeError):
    """The application DB has not received the ledger migration."""


@dataclass(frozen=True, slots=True)
class LedgerImportResult:
    events_inserted: int
    events_unchanged: int
    prices_inserted: int
    prices_unchanged: int
    meta_inserted: int
    meta_unchanged: int


@dataclass(frozen=True, slots=True)
class LedgerApplyResult:
    append_head: int
    event_ids: tuple[str, ...]


class LedgerStoreService:
    """Load and mutate one ledger while preserving physical append order."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def load(self) -> PortfolioLedgerDocument:
        document, _append_head_value = self.load_with_head()
        return document

    def load_with_head(self) -> tuple[PortfolioLedgerDocument, int]:
        """Read one replay document and its physical append head consistently."""
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            _require_schema(connection)
            connection.execute("BEGIN")
            return _load_document(connection), _append_head(connection)

    def append_head(self) -> int:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            _require_schema(connection)
            return _append_head(connection)

    def import_document(self, document: PortfolioLedgerDocument) -> LedgerImportResult:
        """Create missing legacy rows, accepting only byte-stable canonical matches."""
        _require_canonical_prices(document)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            _require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = _import_document(connection, document)
                # Reconstruct through the same DB view used by production reads.
                if _load_document(connection) != document:
                    raise LedgerConflictError("imported ledger does not equal the source document")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return result

    def apply_document(
        self,
        *,
        expected_head: int,
        expected_document: PortfolioLedgerDocument,
        replacement: PortfolioLedgerDocument,
    ) -> LedgerApplyResult:
        """Atomically append events and replace document rows after a stale check.

        Existing events are immutable. A past-time event is physically appended and
        receives the last ordinal at that instant; only the replay view is reordered.
        """
        _require_canonical_prices(expected_document)
        _require_canonical_prices(replacement)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            _require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                current_head = _append_head(connection)
                current = _load_document(connection)
                if current_head != expected_head or current != expected_document:
                    raise LedgerConflictError("stale ledger draft")
                current_by_id = {event.event_id: event for event in current.events}
                replacement_by_id = {event.event_id: event for event in replacement.events}
                if current_by_id.keys() - replacement_by_id.keys():
                    raise LedgerConflictError("ledger events cannot be removed")
                for event_id, event in current_by_id.items():
                    if replacement_by_id[event_id] != event:
                        raise LedgerConflictError(f"ledger event is immutable: {event_id}")

                additions = tuple(
                    event for event in replacement.events if event.event_id not in current_by_id
                )
                _append_events(connection, additions)
                _replace_prices(connection, replacement.market_prices)
                _replace_meta(connection, replacement)
                stored = _load_document(connection)
                if stored != replacement:
                    raise LedgerConflictError(
                        "replacement order does not match append-only replay ordering"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return LedgerApplyResult(
            append_head=current_head + len(additions),
            event_ids=tuple(event.event_id for event in additions),
        )


def load_ledger_in_transaction(
    connection: sqlite3.Connection,
) -> tuple[PortfolioLedgerDocument, int]:
    """Read the canonical ledger through an existing application-DB transaction."""
    _require_schema(connection)
    return _load_document(connection), _append_head(connection)


def _require_schema(connection: sqlite3.Connection) -> None:
    found = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    required = {"ledger_event", "ledger_market_price", "ledger_meta"}
    if not required <= found:
        raise LedgerSchemaError("application DB ledger migration is not installed")


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _event_payload(event: LedgerEvent) -> str:
    return canonical_json(event.model_dump(mode="json"))


def _price_payload(price: MarketPrice) -> str:
    return canonical_json(price.model_dump(mode="json"))


def _meta_payload(document: PortfolioLedgerDocument) -> str:
    return canonical_json(
        {
            "schema_version": document.schema_version,
            "portfolio_scope": document.portfolio_scope,
            "as_of": document.as_of,
            "estimated_exit_tax_rate_bps": document.estimated_exit_tax_rate_bps,
            "estimated_exit_tax_basis": document.estimated_exit_tax_basis,
            "overrides": [item.model_dump(mode="json") for item in document.overrides],
        }
    )


def _event_ticker(event: LedgerEvent) -> str | None:
    ticker = getattr(event, "ticker", None)
    return cast(str | None, ticker)


def _event_proposal_id(event: LedgerEvent) -> str | None:
    reference = getattr(event, "decision_reference", None)
    return reference if isinstance(reference, str) and reference.startswith("prop-") else None


def _same_instant_orders(events: Sequence[LedgerEvent]) -> list[int]:
    seen: Counter[str] = Counter()
    orders: list[int] = []
    for event in events:
        occurred_at = _utc_text(event.occurred_at)
        orders.append(seen[occurred_at])
        seen[occurred_at] += 1
    return orders


def _import_document(
    connection: sqlite3.Connection, document: PortfolioLedgerDocument
) -> LedgerImportResult:
    event_inserted = event_unchanged = price_inserted = price_unchanged = 0
    orders = _same_instant_orders(document.events)
    source_event_ids = {event.event_id for event in document.events}
    stored_event_ids = {
        str(row[0]) for row in connection.execute("SELECT event_id FROM ledger_event")
    }
    extras = stored_event_ids - source_event_ids
    if extras:
        raise LedgerConflictError(f"DB contains events absent from import: {sorted(extras)}")
    for append_seq, (event, same_order) in enumerate(
        zip(document.events, orders, strict=True), start=1
    ):
        values = _event_values(append_seq, event, same_order)
        row = connection.execute(
            """
            SELECT append_seq, event_id, occurred_at, same_instant_order,
                   event_type, ticker, proposal_id, payload
            FROM ledger_event WHERE event_id = ?
            """,
            (event.event_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO ledger_event(
                    append_seq, event_id, occurred_at, same_instant_order,
                    event_type, ticker, proposal_id, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            event_inserted += 1
        elif tuple(row) == values:
            event_unchanged += 1
        else:
            raise LedgerConflictError(f"ledger event conflicts: {event.event_id}")

    source_tickers = {price.ticker for price in document.market_prices}
    stored_tickers = {
        str(row[0]) for row in connection.execute("SELECT ticker FROM ledger_market_price")
    }
    extra_tickers = stored_tickers - source_tickers
    if extra_tickers:
        raise LedgerConflictError(
            f"DB contains market prices absent from import: {sorted(extra_tickers)}"
        )
    for price in document.market_prices:
        values = _price_values(price)
        row = connection.execute(
            """
            SELECT ticker, observed_at, price_yen, source_kind, price_basis,
                   source_ref, payload
            FROM ledger_market_price WHERE ticker = ?
            """,
            (price.ticker,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO ledger_market_price(
                    ticker, observed_at, price_yen, source_kind, price_basis,
                    source_ref, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            price_inserted += 1
        elif tuple(row) == values:
            price_unchanged += 1
        else:
            raise LedgerConflictError(f"market price conflicts: {price.ticker}")

    meta_values = _meta_values(document)
    meta_row = connection.execute(
        """
        SELECT singleton, schema_version, portfolio_scope, as_of,
               estimated_exit_tax_rate_bps, estimated_exit_tax_basis, payload
        FROM ledger_meta WHERE singleton = 1
        """
    ).fetchone()
    if meta_row is None:
        connection.execute(
            """
            INSERT INTO ledger_meta(
                singleton, schema_version, portfolio_scope, as_of,
                estimated_exit_tax_rate_bps, estimated_exit_tax_basis, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            meta_values,
        )
        meta_inserted = 1
        meta_unchanged = 0
    elif tuple(meta_row) == meta_values:
        meta_inserted = 0
        meta_unchanged = 1
    else:
        raise LedgerConflictError("ledger meta conflicts")
    return LedgerImportResult(
        event_inserted,
        event_unchanged,
        price_inserted,
        price_unchanged,
        meta_inserted,
        meta_unchanged,
    )


def _event_values(
    append_seq: int, event: LedgerEvent, same_instant_order: int
) -> tuple[object, ...]:
    return (
        append_seq,
        event.event_id,
        _utc_text(event.occurred_at),
        same_instant_order,
        event.type,
        _event_ticker(event),
        _event_proposal_id(event),
        _event_payload(event),
    )


def _price_values(price: MarketPrice) -> tuple[object, ...]:
    return (
        price.ticker,
        _utc_text(price.observed_at),
        str(price.price_yen),
        price.source_kind,
        price.price_basis,
        price.source_ref,
        _price_payload(price),
    )


def _meta_values(document: PortfolioLedgerDocument) -> tuple[object, ...]:
    return (
        1,
        document.schema_version,
        document.portfolio_scope,
        document.as_of.isoformat(),
        document.estimated_exit_tax_rate_bps,
        document.estimated_exit_tax_basis,
        _meta_payload(document),
    )


def _append_head(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute("SELECT coalesce(max(append_seq), 0) FROM ledger_event").fetchone()[0]
    )


def _append_events(connection: sqlite3.Connection, events: Sequence[LedgerEvent]) -> None:
    append_seq = _append_head(connection)
    next_orders: dict[str, int] = {}
    for event in events:
        occurred_at = _utc_text(event.occurred_at)
        if occurred_at not in next_orders:
            row = connection.execute(
                """
                SELECT coalesce(max(same_instant_order), -1)
                FROM ledger_event WHERE occurred_at = ?
                """,
                (occurred_at,),
            ).fetchone()
            next_orders[occurred_at] = int(row[0]) + 1
        same_order = next_orders[occurred_at]
        next_orders[occurred_at] += 1
        append_seq += 1
        try:
            connection.execute(
                """
                INSERT INTO ledger_event(
                    append_seq, event_id, occurred_at, same_instant_order,
                    event_type, ticker, proposal_id, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _event_values(append_seq, event, same_order),
            )
        except sqlite3.IntegrityError as error:
            raise LedgerConflictError(f"ledger event conflicts: {event.event_id}") from error


def _replace_prices(connection: sqlite3.Connection, prices: Sequence[MarketPrice]) -> None:
    connection.execute("DELETE FROM ledger_market_price")
    connection.executemany(
        """
        INSERT INTO ledger_market_price(
            ticker, observed_at, price_yen, source_kind, price_basis, source_ref, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [_price_values(price) for price in prices],
    )


def _replace_meta(connection: sqlite3.Connection, document: PortfolioLedgerDocument) -> None:
    connection.execute(
        """
        UPDATE ledger_meta
        SET schema_version = ?, portfolio_scope = ?, as_of = ?,
            estimated_exit_tax_rate_bps = ?, estimated_exit_tax_basis = ?, payload = ?
        WHERE singleton = 1
        """,
        _meta_values(document)[1:],
    )


def _load_document(connection: sqlite3.Connection) -> PortfolioLedgerDocument:
    meta = connection.execute("SELECT payload FROM ledger_meta WHERE singleton = 1").fetchone()
    if meta is None:
        raise LedgerConflictError("ledger has not been imported")
    raw = cast(dict[str, object], json.loads(str(meta[0])))
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
    _require_canonical_prices(document)
    return document


def _require_canonical_prices(document: PortfolioLedgerDocument) -> None:
    if any(price.source_kind == "test_fixture" for price in document.market_prices):
        raise LedgerConflictError("canonical application DB cannot use test_fixture prices")


__all__ = [
    "LedgerApplyResult",
    "LedgerConflictError",
    "LedgerImportResult",
    "LedgerSchemaError",
    "LedgerStoreService",
]
