"""Shared SQLite kernel for the single physical store (`market.sqlite`).

Market owns the schema, connection, source-coverage bookkeeping, normalization
primitives, and the price/calendar ingest. Screening reuses this kernel for its
fundamentals/regulation tables without market depending on screening.
"""

from .convert import _optional_date, _optional_float
from .coverage import _daily_bars_covered_by_data, _range_covered
from .jquants import store_jquants_daily_bars, store_jquants_market_calendar
from .schema import (
    SCHEMA_VERSION,
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    _connect_current,
    open_connection,
    validate_current_schema,
)

__all__ = [
    "SCHEMA_VERSION",
    "SQLITE_SCHEMA_VERSION",
    "SQLiteSchemaError",
    "_connect_current",
    "_daily_bars_covered_by_data",
    "_optional_date",
    "_optional_float",
    "_range_covered",
    "open_connection",
    "store_jquants_daily_bars",
    "store_jquants_market_calendar",
    "validate_current_schema",
]
