"""Read-only application DB views for portfolio operations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import cast

from baibai_engine.appdb.read import connect_read_only
from baibai_engine.position.ledger import (
    PortfolioLedgerDocument,
    PortfolioLedgerError,
    PortfolioSnapshot,
    replay_events_through,
    summarize_portfolio,
)
from baibai_engine.position.valuation import current_portfolio
from baibai_engine.read_api.sqlite import (
    is_unwritten_store,
)
from baibai_engine.read_api.sqlite import (
    read_application_rows as read_rows,
)

__all__ = [
    "PortfolioLedgerError",
    "PortfolioSnapshot",
    "current_portfolio",
    "list_portfolio_outcome_payloads",
    "portfolio_ledger_document",
    "replay_events_through",
    "summarize_portfolio",
]


def list_portfolio_outcome_payloads(db_path: Path) -> list[dict[str, object]]:
    """Return immutable outcomes newest first; a missing store is empty."""
    rows = read_rows(
        db_path,
        "SELECT outcome_id, payload FROM portfolio_outcome "
        "ORDER BY period_end_date DESC, outcome_id DESC",
    )
    return [_publication(str(row["outcome_id"]), str(row["payload"])) for row in rows]


def portfolio_ledger_document(db_path: Path) -> PortfolioLedgerDocument | None:
    """Reconstruct the ledger through a query-only SQLite connection."""
    if not db_path.is_file():
        return None
    try:
        with closing(connect_read_only(db_path)) as connection:
            connection.execute("BEGIN")
            meta = connection.execute(
                "SELECT payload FROM ledger_meta WHERE singleton = 1"
            ).fetchone()
            if meta is None:
                return None
            raw = cast(dict[str, object], json.loads(str(meta["payload"])))
            raw["events"] = [
                json.loads(str(row["payload"]))
                for row in connection.execute(
                    "SELECT payload FROM ledger_event "
                    "ORDER BY occurred_at, same_instant_order, append_seq"
                )
            ]
            raw["market_prices"] = [
                json.loads(str(row["payload"]))
                for row in connection.execute(
                    "SELECT payload FROM ledger_market_price ORDER BY ticker"
                )
            ]
    except sqlite3.OperationalError as error:
        if not is_unwritten_store(error):
            raise
        return None
    return PortfolioLedgerDocument.model_validate(raw)


def _publication(outcome_id: str, encoded: str) -> dict[str, object]:
    payload = json.loads(encoded)
    if not isinstance(payload, Mapping):
        raise ValueError("portfolio outcome payload must be an object")
    return {"outcome_id": outcome_id, **payload}
