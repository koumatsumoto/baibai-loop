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

from .macro import (
    latest_macro_context_payload,
    list_macro_context_payloads,
    macro_context_payload,
    macro_indicator_series,
)
from .research import (
    list_holding_review_payloads,
    list_holding_review_publications,
    list_research_packet_payloads,
    list_research_packet_publications,
    list_research_review_payloads,
    list_research_review_publications,
    research_packet_payload,
    research_packet_publication,
)
from .screening import (
    screening_run_payload,
    screening_run_payloads,
    screening_selection_payloads,
)
from .shortlist import list_reviewed_shortlist_payloads
from .tasks import list_task_payloads, task_store_exists

__all__ = [
    "DecisionPacketError",
    "HoldingSnapshot",
    "PortfolioLedgerError",
    "PortfolioSnapshot",
    "latest_macro_context_payload",
    "list_holding_review_payloads",
    "list_holding_review_publications",
    "list_macro_context_payloads",
    "list_research_packet_payloads",
    "list_research_packet_publications",
    "list_research_review_payloads",
    "list_research_review_publications",
    "list_reviewed_shortlist_payloads",
    "list_task_payloads",
    "load_decision_packet",
    "load_portfolio_ledger",
    "macro_context_payload",
    "macro_indicator_series",
    "reconcile_portfolio",
    "research_packet_payload",
    "research_packet_publication",
    "safe_load",
    "screening_run_payload",
    "screening_run_payloads",
    "screening_selection_payloads",
    "task_store_exists",
]
