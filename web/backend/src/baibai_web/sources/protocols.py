"""表示builderの小さな読取契約。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from baibai_engine.portfolio.ledger.events import PortfolioSnapshot


@dataclass(frozen=True, slots=True)
class PortfolioRead:
    snapshot: PortfolioSnapshot
    valuation_error: str | None = None
    holding_quantity_unresolved: bool = False


class LedgerSource(Protocol):
    def snapshot(self) -> PortfolioRead | None:
        """Return None only for an uninitialized ledger; damaged ledgers raise."""
        ...


class MarketSource(Protocol):
    def security_names(self, *, as_of: date, tickers: tuple[str, ...]) -> Mapping[str, str]: ...
