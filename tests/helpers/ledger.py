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
    load_portfolio_ledger_with_sha256,
)


def load_portfolio_ledger(path: Path) -> PortfolioLedgerDocument:
    """Parse a portfolio ledger and drop the source hash."""

    document, _source_sha256 = load_portfolio_ledger_with_sha256(path)
    return document
