"""Legacy ledger import and cutover parity checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from baibai_engine.position.ledger import (
    PortfolioLedgerDocument,
    load_portfolio_ledger,
    reconcile_portfolio,
)
from baibai_engine.position.store import LedgerImportResult, LedgerStoreService


@dataclass(frozen=True, slots=True)
class LedgerParityResult:
    event_order_matches: bool
    snapshot_matches: bool
    market_prices_match: bool
    overrides_match: bool
    meta_matches: bool

    @property
    def matches(self) -> bool:
        return all(
            (
                self.event_order_matches,
                self.snapshot_matches,
                self.market_prices_match,
                self.overrides_match,
                self.meta_matches,
            )
        )


def import_ledger_file(source: Path, *, db_path: Path | None = None) -> LedgerImportResult:
    """Import the strict legacy document without changing its identifiers."""
    return LedgerStoreService(db_path).import_document(load_portfolio_ledger(source))


def check_ledger_parity(
    legacy: PortfolioLedgerDocument, stored: PortfolioLedgerDocument
) -> LedgerParityResult:
    """Compare the three ledger cutover gates without weakening replay semantics."""
    legacy_prices = {item.ticker: item for item in legacy.market_prices}
    stored_prices = {item.ticker: item for item in stored.market_prices}
    return LedgerParityResult(
        event_order_matches=(
            tuple(item.event_id for item in legacy.events)
            == tuple(item.event_id for item in stored.events)
        ),
        snapshot_matches=reconcile_portfolio(legacy) == reconcile_portfolio(stored),
        market_prices_match=legacy_prices == stored_prices,
        overrides_match=legacy.overrides == stored.overrides,
        meta_matches=(
            legacy.as_of == stored.as_of
            and legacy.estimated_exit_tax_rate_bps == stored.estimated_exit_tax_rate_bps
            and legacy.estimated_exit_tax_basis == stored.estimated_exit_tax_basis
        ),
    )


def import_and_check_ledger(
    source: Path, *, db_path: Path | None = None
) -> tuple[LedgerImportResult, LedgerParityResult]:
    legacy = load_portfolio_ledger(source)
    result = LedgerStoreService(db_path).import_document(legacy)
    parity = check_ledger_parity(legacy, LedgerStoreService(db_path).load())
    if not parity.matches:
        raise RuntimeError(f"ledger parity failed: {parity}")
    return result, parity


__all__ = [
    "LedgerParityResult",
    "check_ledger_parity",
    "import_and_check_ledger",
    "import_ledger_file",
]
