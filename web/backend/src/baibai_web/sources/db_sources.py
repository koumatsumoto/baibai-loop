"""保有・タスク・銘柄名の読取。旧Screeningや判断一覧には依存しない。"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.storage.sqlite.read import read_rows
from baibai_engine.market.jpx_reader import read_next_earnings_dates
from baibai_engine.market.jquants_reader import read_master_snapshot
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument, PortfolioSnapshot
from baibai_engine.portfolio.ledger.read import load_ledger_document
from baibai_engine.portfolio.holdings.valuation import current_portfolio
from baibai_engine.storage.application.read import connect_read_only
from baibai_engine.tasks.models import Task
from baibai_engine.tasks.service import TaskService


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

    def exists(self) -> bool:
        return self._document() is not None

    def snapshot(self) -> PortfolioSnapshot:
        document = self._document()
        if document is None:
            raise ValueError("portfolio ledger has not been initialized")
        return current_portfolio(
            document, sqlite_path=self._market_path, now=datetime.now().astimezone()
        )


class DbMarketSource:
    def __init__(self, market_db_path: Path) -> None:
        self._path = market_db_path

    def security_names(self, *, as_of: date) -> Mapping[str, str]:
        snapshot = read_master_snapshot(self._path, as_of=as_of)
        if snapshot is None:
            return {}
        return {
            ticker: str(row["name"])
            for ticker, row in snapshot.securities.items()
            if row.get("name")
        }

    def next_earnings_dates(self, tickers: list[str], *, as_of: date) -> Mapping[str, date] | None:
        if not tickers:
            return {}
        dates = read_next_earnings_dates(self._path, as_of)
        return (
            None
            if dates is None
            else {ticker: dates[ticker] for ticker in tickers if ticker in dates}
        )


class DbTaskSource:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    def exists(self) -> bool:
        return bool(
            read_rows(
                self._path,
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='task'",
                connector=connect_read_only,
            )
        )

    def list_tasks(self) -> list[Task]:
        return TaskService(self._path).list()
