"""SQLite ingest for screening fundamentals/regulation inputs.

The schema, connection, and source-coverage kernel plus the price/calendar ingest
live in `baibai_loop.market.sqlite` (the single physical `market.sqlite` store).
This package owns the screening-specific upsert helpers (master snapshot, fin
summaries, earnings calendar, EDINET, JPX) and re-exposes the shared kernel so
screening callers keep one ingest facade.
"""

from baibai_loop.market.sqlite import (
    SCHEMA_VERSION,
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    open_connection,
    store_jquants_daily_bars,
    store_jquants_market_calendar,
    validate_current_schema,
)

from .edinet import store_edinet_documents, store_edinet_metrics
from .jpx import store_jpx_regulations
from .jquants import (
    store_jquants_earnings_calendar,
    store_jquants_fin_summaries,
    store_jquants_master,
)

__all__ = [
    "SCHEMA_VERSION",
    "SQLITE_SCHEMA_VERSION",
    "SQLiteSchemaError",
    "open_connection",
    "store_edinet_documents",
    "store_edinet_metrics",
    "store_jpx_regulations",
    "store_jquants_daily_bars",
    "store_jquants_earnings_calendar",
    "store_jquants_fin_summaries",
    "store_jquants_market_calendar",
    "store_jquants_master",
    "validate_current_schema",
]
