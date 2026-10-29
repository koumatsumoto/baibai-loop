"""表示builderの小さな読取契約。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from baibai_engine.portfolio.ledger.events import PortfolioSnapshot
from baibai_engine.tasks.models import Task


@dataclass(frozen=True, slots=True)
class PortfolioRead:
    snapshot: PortfolioSnapshot
    valuation_error: str | None = None


class LedgerSource(Protocol):
    def exists(self) -> bool: ...
    def snapshot(self) -> PortfolioRead: ...


class LedgerFactsSource(Protocol):
    def exists(self) -> bool: ...
    def facts_snapshot(self) -> PortfolioSnapshot: ...


class MarketSource(Protocol):
    def security_names(self, *, as_of: date) -> Mapping[str, str]: ...
    def next_earnings_dates(
        self, tickers: list[str], *, as_of: date
    ) -> Mapping[str, date] | None: ...


class TaskSource(Protocol):
    def exists(self) -> bool: ...
    def list_tasks(self) -> list[Task]: ...
