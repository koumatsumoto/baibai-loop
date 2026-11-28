"""保有・銘柄名の読取。旧Screeningや判断一覧には依存しない。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from baibai_engine.machine_analysis.observations.stocks.current_reader import (
    CurrentListingSection,
    read_current_jp_master,
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
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.read import read_store_if_written
from baibai_engine.storage.sqlite.source_cache.read import (
    connect_read_only as connect_market_read_only,
)
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_web.sources.protocols import PortfolioRead


class DbLedgerSource:
    def __init__(self, db_path: Path, market_db_path: Path) -> None:
        self._path = db_path
        self._market_path = market_db_path

    def _document(self) -> PortfolioLedgerDocument | None:
        def load() -> PortfolioLedgerDocument | None:
            with closing(connect_read_only(self._path)) as connection:
                connection.execute("BEGIN")
                return load_ledger_document(connection)

        return read_store_if_written(self._path, load)

    def snapshot(self, *, now: datetime) -> PortfolioRead | None:
        document = self._document()
        if document is None:
            return None
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
            return PortfolioRead(snapshot, str(error), holding_quantity_unresolved=True)
        except (OSError, SQLiteSchemaError, sqlite3.Error, ValueError):
            # Only this presentation boundary may retain unvalued ledger facts on a
            # market read failure. Judgment readers still reject the broken store.
            state = replay_events_through(document.events, now)
            return PortfolioRead(
                summarize_portfolio(document, state, {}), "市場評価を読み取れません。"
            )
        return PortfolioRead(snapshot)


class CurrentJpMarketSource:
    """Use one caller-pinned listing section; missing names do not read an older master."""

    def __init__(
        self, market_db_path: Path, *, listing: CurrentListingSection | None = None
    ) -> None:
        self._path = market_db_path
        self._listing = listing

    def security_names(self, *, at: datetime, tickers: tuple[str, ...]) -> Mapping[str, str]:
        if self._listing is None:
            return {}

        def load() -> Mapping[str, str]:
            with closing(connect_market_read_only(self._path)) as calendar:
                calendar.execute("BEGIN")
                snapshot = read_current_jp_master(
                    self._listing,
                    calendar=calendar,
                    as_of=at.astimezone(JST).date(),
                    evaluation_time=at,
                    tickers=tickers,
                )
            if snapshot is None:
                return {}
            return {ticker: security.name for ticker, security in snapshot.securities.items()}

        return read_store_if_written(self._path, load) or {}
