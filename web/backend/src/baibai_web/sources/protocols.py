"""表示builderの小さな読取契約。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from baibai_engine.portfolio.ledger.events import PortfolioSnapshot


@dataclass(frozen=True, slots=True)
class PortfolioRead:
    snapshot: PortfolioSnapshot
    valuation_error: str | None = None
    holding_quantity_unresolved: bool = False


class LedgerSource(Protocol):
    def snapshot(self, *, now: datetime) -> PortfolioRead | None:
        """Replay and value at the dashboard's clock. Only an uninitialized ledger is absent."""
        ...


class MarketSource(Protocol):
    def security_names(self, *, at: datetime, tickers: tuple[str, ...]) -> Mapping[str, str]: ...
