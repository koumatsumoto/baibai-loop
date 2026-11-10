"""保有・銘柄名の読取。旧Screeningや判断一覧には依存しない。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_master_snapshot,
)
from baibai_engine.portfolio.holdings.valuation import (
    UnresolvedHoldingQuantityError,
    current_portfolio,
)
from baibai_engine.portfolio.ledger.events import (
    PortfolioLedgerDocument,
    replay_events_through,
    summarize_portfolio,
)
from baibai_engine.portfolio.ledger.read import load_ledger_document
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_web.sources.protocols import PortfolioRead


class DbLedgerSource:
    def __init__(self, db_path: Path, market_db_path: Path) -> None:
        self._path = db_path
        self._market_path = market_db_path

    def _document(self) -> PortfolioLedgerDocument | None:
        if not self._path.is_file():
            return None
        with closing(connect_read_only(self._path)) as connection:
            connection.execute("BEGIN")
            return load_ledger_document(connection)

    def snapshot(self) -> PortfolioRead | None:
        document = self._document()
        if document is None:
            return None
        now = datetime.now().astimezone()
        try:
            snapshot = current_portfolio(document, sqlite_path=self._market_path, now=now)
        except UnresolvedHoldingQuantityError as error:
            state = replay_events_through(document.events, now)
            snapshot = replace(
                summarize_portfolio(document, state, {}),
                holdings_market_value_yen=None,
                total_capital_yen=None,
                realized_gross_pnl_yen=None,
                estimated_exit_tax_yen=None,
                warnings=(),
            )
            return PortfolioRead(snapshot, str(error))
        except (SQLiteSchemaError, sqlite3.Error, ValueError):
            # Only this presentation boundary may retain unvalued ledger facts on a
            # market read failure. Judgment readers still reject the broken store.
            state = replay_events_through(document.events, now)
            return PortfolioRead(
                summarize_portfolio(document, state, {}), "市場評価を読み取れません。"
            )
        return PortfolioRead(snapshot)


class DbMarketSource:
    def __init__(self, market_db_path: Path) -> None:
        self._path = market_db_path

    def security_names(self, *, as_of: date) -> Mapping[str, str]:
        snapshot = read_master_snapshot(self._path, as_of=as_of)
        if snapshot is None:
            return {}
        return {ticker: security.name for ticker, security in snapshot.securities.items()}
