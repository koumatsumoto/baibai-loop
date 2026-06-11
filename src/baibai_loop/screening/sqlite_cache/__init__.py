"""SQLite canonical store for screening inputs.

SQLite is the local source of truth for screening inputs. Provider fetch paths
write normalized rows and source coverage directly into this database.
This package owns:

- the current-only SQLite schema (versioned via `PRAGMA user_version`)
- direct per-source upsert helpers used by providers
- `source_coverage`, which records request windows that must be present before
  `screening run` can execute
"""

from .edinet import store_edinet_documents, store_edinet_metrics
from .jpx import store_jpx_regulations
from .jquants import (
    store_jquants_daily_bars,
    store_jquants_earnings_calendar,
    store_jquants_fin_summaries,
    store_jquants_market_calendar,
    store_jquants_master,
)
from .schema import (
    SCHEMA_VERSION,
    SQLITE_SCHEMA_VERSION,
    SQLiteCacheError,
    SQLiteSchemaError,
    open_connection,
    validate_current_schema,
)

__all__ = [
    "SCHEMA_VERSION",
    "SQLITE_SCHEMA_VERSION",
    "SQLiteCacheError",
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
