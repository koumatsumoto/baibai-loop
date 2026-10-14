"""Query-only facade for read paths: :mod:`baibai_web`, the batch, and engine CLIs.

Writable connections, schema cutovers, providers, and CLI composition deliberately
remain outside this package. The re-exports expose DB-backed queries and the
domain models needed to assemble read-only application views. What is re-exported
here tracks what is imported through the facade; a query with no facade consumer
stays reachable at its own module.
"""

from baibai_engine.foundation.repository_layout import (
    APPLICATION_DB_PATH,
    ER_LEVEL_CALIBRATION_CONTEXT_PATH,
    INDICATORS_DB_PATH,
    MARKET_DB_PATH,
    RUNS_DB_PATH,
    StoreLayoutError,
    reject_noncanonical_store_paths,
    repository_root_error,
)
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market_context.master import read_master_snapshot as market_master_snapshot
from baibai_engine.position.ledger import (
    HoldingSnapshot,
    PortfolioLedgerError,
    PortfolioSnapshot,
)

from .capital_allocation import (
    capital_allocation_assessment_payload,
    list_capital_allocation_assessment_payloads,
)
from .freshness import (
    application_db_updated_at,
    indicator_latest_observed_at,
    screening_latest_asof,
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
from .market_context import (
    ASSESSMENT_STALE_DAYS,
    READING_RULES_PATH,
    IndicatorGranularity,
    assessment_payload,
    indicator_fetch_health,
    indicator_series,
    indicator_series_names,
    latest_assessment_payload,
    list_assessment_payloads,
    reading_snapshot,
    registered_indicator,
)
from .materialization import (
    MaterializationPreconditionError,
    validate_application_store_schema,
    validate_market_store_hydration,
    validate_market_store_schema,
    validate_reading_rules,
)
from .operations import list_operation_sessions
from .position import list_portfolio_outcome_payloads, portfolio_ledger_document
from .research import (
    list_position_review_publications,
    list_thesis_publications,
    list_thesis_review_publications,
    position_review_null_reason,
    thesis_publication,
)
from .screening import (
    candidate_rows,
    interpreted_next_earnings_status,
    previous_run_revision_id,
    reinvestment_coordinates,
    screening_calibration_method_identity,
    screening_candidates_payload,
    screening_run_asof_dates,
    screening_run_payload,
)
from .tasks import list_task_payloads, task_store_exists

__all__ = [
    "APPLICATION_DB_PATH",
    "ASSESSMENT_STALE_DAYS",
    "ER_LEVEL_CALIBRATION_CONTEXT_PATH",
    "INDICATORS_DB_PATH",
    "MARKET_DB_PATH",
    "READING_RULES_PATH",
    "RUNS_DB_PATH",
    "HoldingSnapshot",
    "IndicatorGranularity",
    "MaterializationPreconditionError",
    "PortfolioLedgerError",
    "PortfolioSnapshot",
    "StoreLayoutError",
    "StoredMachineValuation",
    "anchor_gap_pct",
    "application_db_updated_at",
    "assessment_payload",
    "candidate_rows",
    "capital_allocation_assessment_payload",
    "close_change_since",
    "current_portfolio",
    "indicator_fetch_health",
    "indicator_latest_observed_at",
    "indicator_series",
    "indicator_series_names",
    "interpreted_next_earnings_status",
    "latest_assessment_payload",
    "latest_disclosure_dates_after",
    "latest_market_bar_date",
    "latest_unadjusted_closes",
    "list_assessment_payloads",
    "list_capital_allocation_assessment_payloads",
    "list_operation_sessions",
    "list_portfolio_outcome_payloads",
    "list_position_review_publications",
    "list_task_payloads",
    "list_thesis_publications",
    "list_thesis_review_publications",
    "machine_valuation_from_analysis",
    "market_calendar_business_day",
    "market_master_snapshot",
    "next_earnings_dates",
    "portfolio_ledger_document",
    "position_review_null_reason",
    "previous_business_day",
    "previous_run_revision_id",
    "reading_snapshot",
    "registered_indicator",
    "reinvestment_coordinates",
    "reject_noncanonical_store_paths",
    "repository_root_error",
    "reviewed_thesis_projection",
    "safe_load",
    "screening_calibration_method_identity",
    "screening_candidates_payload",
    "screening_latest_asof",
    "screening_run_asof_dates",
    "screening_run_payload",
    "stored_machine_valuation",
    "task_store_exists",
    "thesis_publication",
    "validate_application_store_schema",
    "validate_market_store_hydration",
    "validate_market_store_schema",
    "validate_reading_rules",
    "worst_close_drawdown",
]

from baibai_engine.read_api.position import current_portfolio

from .research import reviewed_thesis_projection
from .valuation import (
    StoredMachineValuation,
    anchor_gap_pct,
    machine_valuation_from_analysis,
    stored_machine_valuation,
)
