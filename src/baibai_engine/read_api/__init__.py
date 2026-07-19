"""Query-only facade imported by :mod:`baibai_app`.

Writable connections, migrations, providers, and CLI composition deliberately
remain outside this package.  The re-exports below are the temporary YAML read
surface until each domain moves to its database-backed query implementation.
"""

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import (
    HoldingSnapshot,
    PortfolioLedgerError,
    PortfolioSnapshot,
    load_portfolio_ledger,
    reconcile_portfolio,
)
from baibai_engine.research.decision_packet import DecisionPacketError, load_decision_packet

from .tasks import list_task_payloads, task_store_exists

__all__ = [
    "DecisionPacketError",
    "HoldingSnapshot",
    "PortfolioLedgerError",
    "PortfolioSnapshot",
    "list_task_payloads",
    "load_decision_packet",
    "load_portfolio_ledger",
    "reconcile_portfolio",
    "safe_load",
    "task_store_exists",
]
