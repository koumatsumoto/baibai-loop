"""Read a ledger fixture the way a test wants it: the document, without the hash.

``position.ledger`` hands every production caller the bytes' SHA-256 alongside the
parsed document, because recording an outcome has to name the exact ledger it read.
A test that only needs the parsed content would otherwise index the pair at every
call site.
"""

from __future__ import annotations

from pathlib import Path

from baibai_engine.position.ledger import (
    PortfolioLedgerDocument,
    PortfolioSnapshot,
    load_portfolio_ledger_with_sha256,
    replay_events_through,
    summarize_portfolio,
)
from baibai_engine.position.policy import PORTFOLIO_POLICY, PolicyConfig


def load_portfolio_ledger(path: Path) -> PortfolioLedgerDocument:
    """Parse a portfolio ledger and drop the source hash."""

    document, _source_sha256 = load_portfolio_ledger_with_sha256(path)
    return document


def portfolio_snapshot(
    document: PortfolioLedgerDocument, *, policy: PolicyConfig = PORTFOLIO_POLICY
) -> PortfolioSnapshot:
    """Compose the replay/summary owners with the fixture's explicit quote input."""
    return summarize_portfolio(
        document,
        replay_events_through(document.events, document.as_of, policy=policy),
        {price.ticker: price for price in document.market_prices},
        policy=policy,
    )
