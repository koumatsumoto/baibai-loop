"""確認済み取引事実のappendとCASを所有し、ledger更新を産む。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.position.ledger import (
    LedgerEvent,
    MarketPrice,
    PortfolioLedgerDocument,
)
from baibai_engine.position.ledger_read import (
    LedgerConflictError,
    ledger_append_head,
    load_ledger_in_transaction,
    require_canonical_prices,
)


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
        if not database_path(self._db_path).is_file():
            raise LedgerConflictError("ledger has not been imported")
        with closing(connect_read_only(self._db_path)) as connection:
            connection.execute("BEGIN")
            return load_ledger_in_transaction(connection)

    def append_head(self) -> int:
        with closing(connect_read_only(self._db_path)) as connection:
            return ledger_append_head(connection)

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
        require_canonical_prices(expected_document)
        require_canonical_prices(replacement)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current, current_head = load_ledger_in_transaction(connection)
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
                stored, _ = load_ledger_in_transaction(connection)
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


def _event_decision_reference(event: LedgerEvent) -> str | None:
    reference = getattr(event, "decision_reference", None)
    return reference if isinstance(reference, str) else None


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
        _event_decision_reference(event),
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


def _append_events(connection: sqlite3.Connection, events: Sequence[LedgerEvent]) -> None:
    append_seq = ledger_append_head(connection)
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
                    event_type, ticker, decision_reference, payload
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


__all__ = [
    "LedgerApplyResult",
    "LedgerStoreService",
]
