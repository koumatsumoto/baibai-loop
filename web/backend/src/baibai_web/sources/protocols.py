"""表示builderの小さな読取契約。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from baibai_engine.portfolio.ledger.events import PortfolioSnapshot
from baibai_web.readmodel.proposal_models import SavedProposalView


@dataclass(frozen=True, slots=True)
class PortfolioRead:
    snapshot: PortfolioSnapshot
    valuation_error: str | None = None


class LedgerSource(Protocol):
    def snapshot(self) -> PortfolioRead | None:
        """Return None only for an uninitialized ledger; damaged ledgers raise."""
        ...


class MarketSource(Protocol):
    def security_names(self, *, as_of: date) -> Mapping[str, str]: ...


@dataclass(frozen=True, slots=True)
class ProposalHistoryRead:
    proposals: tuple[SavedProposalView, ...]
    unavailable_proposal_ids: tuple[str, ...]


class ProposalSource(Protocol):
    def latest(self) -> SavedProposalView | None:
        """Return None only for a valid store with no saved proposal; read failures raise."""
        ...


class ProposalHistorySource(Protocol):
    def history(self) -> ProposalHistoryRead:
        """Read each saved publication independently; no ticker-based replacement."""
        ...
