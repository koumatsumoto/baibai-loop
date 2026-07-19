"""Shared SQLite kernel for the single physical store (`market.sqlite`).

Market owns the schema, connection, source-coverage bookkeeping, normalization
primitives, and the price/calendar ingest. Screening reuses this kernel for its
fundamentals/regulation tables without market depending on screening.

This package `__init__` is the curated public surface of the kernel: the names
re-exported here (and listed in `__all__`) are the only ones other packages may
import. Helpers kept with a leading underscore in the submodules are package-
private and must not be imported across the package boundary.
"""

from .convert import (
    NormalizedRows,
    code_quality,
    date_iso,
    first,
    is_common_stock_flag,
    normalize_ticker_or_none,
    optional_date,
    optional_float,
    to_float,
    to_str_or_none,
)
from .coverage import (
    daily_bars_covered_by_data,
    date_range_row_count,
    delete_date_range,
    delete_overlapping_source_coverage,
    delete_source_coverage,
    range_covered,
    record_range_source_coverage,
    record_source_coverage,
    table_row_count,
)
from .jquants import store_jquants_daily_bars, store_jquants_market_calendar
from .schema import (
    SCHEMA_VERSION,
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    connect_current,
    open_connection,
    validate_current_schema,
)

__all__ = [
    "SCHEMA_VERSION",
    "SQLITE_SCHEMA_VERSION",
    "NormalizedRows",
    "SQLiteSchemaError",
    "code_quality",
    "connect_current",
    "daily_bars_covered_by_data",
    "date_iso",
    "date_range_row_count",
    "delete_date_range",
    "delete_overlapping_source_coverage",
    "delete_source_coverage",
    "first",
    "is_common_stock_flag",
    "normalize_ticker_or_none",
    "open_connection",
    "optional_date",
    "optional_float",
    "range_covered",
    "record_range_source_coverage",
    "record_source_coverage",
    "store_jquants_daily_bars",
    "store_jquants_market_calendar",
    "table_row_count",
    "to_float",
    "to_str_or_none",
    "validate_current_schema",
]
