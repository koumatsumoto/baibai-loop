"""Small ledger state shared by direct-fact and decision-binding tests."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from tests.helpers.db_seed import seed_ledger

NOW = datetime.fromisoformat("2026-10-14T10:00:00+09:00")


def _service(tmp_path: Path) -> tuple[LedgerStoreService, Path]:
    path = tmp_path / "app.sqlite"
    seed_ledger(
        path,
        PortfolioLedgerDocument.model_validate(
            {
                "schema_version": 3,
                "portfolio_scope": "repository_only",
                "as_of": NOW.isoformat(),
                "events": [
                    {
                        "event_id": "synthetic-opening",
                        "type": "opening_balance",
                        "occurred_at": (NOW - timedelta(hours=1)).isoformat(),
                        "amount_yen": 300000,
                    }
                ],
            }
        ),
    )
    return LedgerStoreService(path), path


def _binding_count(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection, connection:
        return connection.execute("SELECT count(*) FROM position_decision_reference").fetchone()[0]
