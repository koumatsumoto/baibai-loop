"""表示builderの小さな読取契約。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from baibai_engine.portfolio.ledger.events import PortfolioSnapshot
from baibai_engine.tasks.models import Task
from baibai_web.readmodel.proposal_models import SavedProposalView


@dataclass(frozen=True, slots=True)
class PortfolioRead:
    snapshot: PortfolioSnapshot
    valuation_error: str | None = None


class LedgerSource(Protocol):
    def snapshot(self) -> PortfolioRead | None:
        """Return None only for an uninitialized ledger; damaged ledgers raise."""
        ...


class LedgerFactsSource(Protocol):
    def facts_snapshot(self) -> PortfolioSnapshot | None:
        """Return ledger facts or None when uninitialized, without market valuation."""
        ...


class MarketSource(Protocol):
    def security_names(self, *, as_of: date) -> Mapping[str, str]: ...
    def next_earnings_dates(
        self, tickers: list[str], *, as_of: date
    ) -> Mapping[str, date] | None: ...


class TaskSource(Protocol):
    def exists(self) -> bool: ...
    def list_tasks(self) -> list[Task]: ...


class ProposalSource(Protocol):
    def latest(self) -> SavedProposalView | None:
        """Return None only for a valid store with no saved proposal; read failures raise."""
        ...
