"""Query-only facade imported by :mod:`baibai_app`.

Writable connections, migrations, providers, and CLI composition deliberately
remain outside this package. The re-exports expose DB-backed queries and the
domain models needed to assemble read-only application views.
"""

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import (
    HoldingSnapshot,
    PortfolioLedgerError,
    PortfolioSnapshot,
    load_portfolio_ledger,
    reconcile_portfolio,
)
from baibai_engine.research.thesis import ThesisError, load_thesis

from .freshness import (
    application_db_updated_at,
    macro_latest_observed_at,
    screening_latest_asof,
)
from .macro import (
    MACRO_CONTEXT_STALE_DAYS,
    MACRO_READING_RULES_PATH,
    MacroGranularity,
    latest_macro_context_payload,
    list_macro_context_payloads,
    macro_context_payload,
    macro_indicator_series,
    macro_reading_snapshot,
    macro_series_names,
)
from .market import (
    latest_unadjusted_closes,
    market_calendar_business_day,
    next_earnings_dates,
)
from .operations import list_operation_sessions, operation_session
from .position import list_portfolio_outcome_payloads, portfolio_ledger_document
from .proposals import list_proposal_payloads
from .research import (
    list_holding_review_payloads,
    list_holding_review_publications,
    list_thesis_payloads,
    list_thesis_publications,
    list_thesis_review_publications,
    thesis_publication,
)
from .screening import (
    previous_run_revision_id,
    screening_run_asof_dates,
    screening_run_payload,
    screening_selection_payloads,
)
from .shortlist import latest_shortlist_payload, list_shortlist_payloads
from .tasks import list_task_payloads, task_store_exists

__all__ = [
    "MACRO_CONTEXT_STALE_DAYS",
    "MACRO_READING_RULES_PATH",
    "HoldingSnapshot",
    "MacroGranularity",
    "PortfolioLedgerError",
    "PortfolioSnapshot",
    "ThesisError",
    "application_db_updated_at",
    "latest_macro_context_payload",
    "latest_shortlist_payload",
    "latest_unadjusted_closes",
    "list_holding_review_payloads",
    "list_holding_review_publications",
    "list_macro_context_payloads",
    "list_operation_sessions",
    "list_portfolio_outcome_payloads",
    "list_proposal_payloads",
    "list_shortlist_payloads",
    "list_task_payloads",
    "list_thesis_payloads",
    "list_thesis_publications",
    "list_thesis_review_publications",
    "load_portfolio_ledger",
    "load_thesis",
    "macro_context_payload",
    "macro_indicator_series",
    "macro_latest_observed_at",
    "macro_reading_snapshot",
    "macro_series_names",
    "market_calendar_business_day",
    "next_earnings_dates",
    "operation_session",
    "portfolio_ledger_document",
    "previous_run_revision_id",
    "reconcile_portfolio",
    "safe_load",
    "screening_latest_asof",
    "screening_run_asof_dates",
    "screening_run_payload",
    "screening_selection_payloads",
    "task_store_exists",
    "thesis_publication",
]
