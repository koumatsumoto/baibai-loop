"""Query-only facade imported by :mod:`baibai_web`.

Writable connections, migrations, providers, and CLI composition deliberately
remain outside this package. The re-exports expose DB-backed queries and the
domain models needed to assemble read-only application views.
"""

from baibai_engine.foundation.repository_layout import (
    APPLICATION_DB_PATH,
    CALIBRATION_DIR,
    ER_LEVEL_CALIBRATION_CONTEXT_PATH,
    MACRO_DB_PATH,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
    StoreLayoutError,
    reject_noncanonical_store_paths,
    repository_root_error,
)
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import (
    HoldingSnapshot,
    PortfolioLedgerError,
    PortfolioSnapshot,
    load_portfolio_ledger,
    reconcile_portfolio,
)
from baibai_engine.research.thesis import ThesisError, load_thesis

from .assessment import (
    bargain_assessment_payload,
    list_bargain_assessment_payloads,
)
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
    macro_context_triggers,
    macro_indicator_series,
    macro_reading_snapshot,
    macro_registered_series,
    macro_series_fetch_health,
    macro_series_names,
)
from .market import (
    close_change_since,
    latest_disclosure_dates_after,
    latest_market_bar_date,
    latest_unadjusted_closes,
    market_calendar_business_day,
    next_earnings_dates,
    previous_business_day,
    worst_close_drawdown,
)
from .materialization import (
    MaterializationPreconditionError,
    validate_application_store_schema,
    validate_macro_reading_rules,
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
    screening_calibration_method_identity,
    screening_run_asof_dates,
    screening_run_payload,
    screening_selection_payloads,
)
from .shortlist import latest_shortlist_payload, list_shortlist_payloads
from .system import (
    ProviderFailureStreak,
    StoreStats,
    application_store_stats,
    never_attempted_series,
    provider_failure_streaks,
    store_stats,
)
from .tasks import list_task_payloads, task_store_exists

__all__ = [
    "APPLICATION_DB_PATH",
    "CALIBRATION_DIR",
    "ER_LEVEL_CALIBRATION_CONTEXT_PATH",
    "MACRO_CONTEXT_STALE_DAYS",
    "MACRO_DB_PATH",
    "MACRO_READING_RULES_PATH",
    "MARKET_DB_PATH",
    "RUNS_DB_PATH",
    "HoldingSnapshot",
    "MacroGranularity",
    "MaterializationPreconditionError",
    "PortfolioLedgerError",
    "PortfolioSnapshot",
    "ProviderFailureStreak",
    "StoreLayoutError",
    "StoreStats",
    "ThesisError",
    "application_db_updated_at",
    "application_store_stats",
    "bargain_assessment_payload",
    "close_change_since",
    "latest_disclosure_dates_after",
    "latest_macro_context_payload",
    "latest_market_bar_date",
    "latest_shortlist_payload",
    "latest_unadjusted_closes",
    "list_bargain_assessment_payloads",
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
    "macro_context_triggers",
    "macro_indicator_series",
    "macro_latest_observed_at",
    "macro_reading_snapshot",
    "macro_registered_series",
    "macro_series_fetch_health",
    "macro_series_names",
    "market_calendar_business_day",
    "never_attempted_series",
    "next_earnings_dates",
    "operation_session",
    "portfolio_ledger_document",
    "previous_business_day",
    "previous_run_revision_id",
    "provider_failure_streaks",
    "reconcile_portfolio",
    "reject_noncanonical_store_paths",
    "repository_root_error",
    "safe_load",
    "screening_calibration_method_identity",
    "screening_latest_asof",
    "screening_run_asof_dates",
    "screening_run_payload",
    "screening_selection_payloads",
    "store_stats",
    "task_store_exists",
    "thesis_publication",
    "validate_application_store_schema",
    "validate_macro_reading_rules",
    "worst_close_drawdown",
]
