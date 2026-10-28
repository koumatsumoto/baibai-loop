"""Ledger fixture loading and test-only composition of explicit valuation inputs."""

from __future__ import annotations

from pathlib import Path

from baibai_engine.portfolio.ledger.events import (
    MarketPrice,
    PortfolioLedgerDocument,
    PortfolioSnapshot,
    replay_events_through,
    summarize_portfolio,
)
from baibai_engine.portfolio.policy import PORTFOLIO_POLICY, PolicyConfig
from baibai_engine.primitives.yaml import safe_load


def load_portfolio_ledger(path: Path) -> PortfolioLedgerDocument:
    """Parse a test fixture; production reads the application database."""
    return PortfolioLedgerDocument.model_validate(safe_load(path.read_text(encoding="utf-8")))


def portfolio_snapshot(
    document: PortfolioLedgerDocument,
    *,
    prices: tuple[MarketPrice, ...] = (),
    policy: PolicyConfig = PORTFOLIO_POLICY,
) -> PortfolioSnapshot:
    """Compose the replay/summary owners with explicit, separately supplied quote inputs."""
    return summarize_portfolio(
        document,
        replay_events_through(document.events, document.as_of, policy=policy),
        {price.ticker: price for price in prices},
        policy=policy,
    )
