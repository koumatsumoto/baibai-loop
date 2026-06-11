"""Current-only SQLite schema: DDL, versioning, and shape validation."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

SQLITE_SCHEMA_VERSION = 11
SCHEMA_VERSION = str(SQLITE_SCHEMA_VERSION)

_REQUIRED_TABLES = (
    "jquants_daily_bars",
    "jquants_fin_summaries",
    "jquants_master_snapshots",
    "jquants_earnings_calendar",
    "jquants_market_calendar",
    "edinet_documents",
    "edinet_metrics",
    "jpx_regulation_flags",
    "jpx_regulation_sources",
    "source_coverage",
)
_REQUIRED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "jquants_daily_bars": (
        "ticker",
        "traded_at",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover_value",
        "adjustment_open",
        "adjustment_high",
        "adjustment_low",
        "adjustment_close",
        "adjustment_volume",
        "adjustment_factor",
        "upper_limit",
        "lower_limit",
    ),
    "jquants_fin_summaries": (
        "ticker",
        "disclosed_at",
        "forecast_eps",
        "eps_ttm",
        "bps",
        "shares_outstanding",
        "sales",
        "cfo",
        "cash_eq",
        "total_assets",
        "equity",
        "operating_profit",
        "ordinary_profit",
        "profit",
        "fiscal_period",
        "fiscal_year_end",
        "period_start",
        "period_end",
    ),
    "jquants_master_snapshots": (
        "snapshot_date",
        "ticker",
        "name",
        "market",
        "sector_33",
        "is_common_stock",
    ),
    "jquants_earnings_calendar": ("announcement_date", "ticker"),
    "jquants_market_calendar": ("day", "is_business_day"),
    "edinet_documents": (
        "doc_date",
        "doc_id",
        "sec_code",
        "doc_type_code",
        "csv_flag",
        "xbrl_flag",
        "legal_status",
        "disclosure_status",
        "withdrawal_status",
        "submit_datetime",
        "doc_description",
        "period_start",
        "period_end",
    ),
    "edinet_metrics": (
        "asof_date",
        "ticker",
        "sales_ttm",
        "ocf_ttm",
        "debt",
        "cash",
        "ebitda_ttm",
        "consolidation_basis",
        "ttm_quality_ev_ebitda",
        "ttm_quality_p_s",
        "ttm_quality_pcfr",
        "operating_profit_ttm",
        "depreciation_and_amortization_ttm",
        "capex_ttm",
        "fcf_ttm",
        "net_cash",
        "equity",
        "total_assets",
        "ttm_quality_fcf",
        "ttm_quality_net_cash",
        "source_doc_id",
        "document_type",
        "source_submit_datetime",
        "source_period_start",
        "source_period_end",
        "capex_source",
        "failure_reasons",
    ),
    "jpx_regulation_flags": (
        "asof_date",
        "source_name",
        "ticker",
        "flag",
        "fetched_at_utc",
    ),
    "jpx_regulation_sources": ("asof_date", "source_name", "fetched_at_utc"),
    "source_coverage": (
        "source",
        "coverage_key",
        "coverage_start",
        "coverage_end",
        "fetched_at_utc",
        "record_count",
        "status",
        "error",
    ),
}
_EXPLICIT_INDEXES: Mapping[str, tuple[str, tuple[str, ...], bool]] = {
    "idx_jquants_daily_bars_traded_at": ("jquants_daily_bars", ("traded_at",), False),
    "idx_source_coverage_source_window": (
        "source_coverage",
        ("source", "coverage_start", "coverage_end"),
        False,
    ),
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jquants_daily_bars(
  ticker TEXT NOT NULL,
  traded_at TEXT NOT NULL,
  open REAL,
  high REAL,
  low REAL,
  close REAL,
  volume REAL,
  turnover_value REAL,
  adjustment_open REAL,
  adjustment_high REAL,
  adjustment_low REAL,
  adjustment_close REAL,
  adjustment_volume REAL,
  adjustment_factor REAL,
  upper_limit TEXT,
  lower_limit TEXT,
  PRIMARY KEY (ticker, traded_at)
);

CREATE INDEX IF NOT EXISTS idx_jquants_daily_bars_traded_at
  ON jquants_daily_bars(traded_at);

CREATE TABLE IF NOT EXISTS jquants_fin_summaries(
  ticker TEXT NOT NULL,
  disclosed_at TEXT NOT NULL,
  forecast_eps REAL,
  eps_ttm REAL,
  bps REAL,
  shares_outstanding REAL,
  sales REAL,
  cfo REAL,
  cash_eq REAL,
  total_assets REAL,
  equity REAL,
  operating_profit REAL,
  ordinary_profit REAL,
  profit REAL,
  fiscal_period TEXT,
  fiscal_year_end TEXT,
  period_start TEXT,
  period_end TEXT,
  PRIMARY KEY (ticker, disclosed_at)
);

CREATE TABLE IF NOT EXISTS jquants_master_snapshots(
  snapshot_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  name TEXT,
  market TEXT,
  sector_33 TEXT,
  is_common_stock INTEGER,
  PRIMARY KEY (snapshot_date, ticker)
);

CREATE TABLE IF NOT EXISTS jquants_earnings_calendar(
  announcement_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  PRIMARY KEY (announcement_date, ticker)
);

CREATE TABLE IF NOT EXISTS jquants_market_calendar(
  day TEXT PRIMARY KEY,
  is_business_day INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS edinet_documents(
  doc_date TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  sec_code TEXT,
  doc_type_code TEXT,
  csv_flag TEXT,
  xbrl_flag TEXT,
  legal_status TEXT,
  disclosure_status TEXT,
  withdrawal_status TEXT,
  submit_datetime TEXT,
  doc_description TEXT,
  period_start TEXT,
  period_end TEXT,
  PRIMARY KEY (doc_date, doc_id)
);

CREATE TABLE IF NOT EXISTS edinet_metrics(
  asof_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  sales_ttm REAL,
  ocf_ttm REAL,
  debt REAL,
  cash REAL,
  ebitda_ttm REAL,
  consolidation_basis TEXT,
  ttm_quality_ev_ebitda TEXT,
  ttm_quality_p_s TEXT,
  ttm_quality_pcfr TEXT,
  operating_profit_ttm REAL,
  depreciation_and_amortization_ttm REAL,
  capex_ttm REAL,
  fcf_ttm REAL,
  net_cash REAL,
  equity REAL,
  total_assets REAL,
  ttm_quality_fcf TEXT,
  ttm_quality_net_cash TEXT,
  source_doc_id TEXT,
  document_type TEXT,
  source_submit_datetime TEXT,
  source_period_start TEXT,
  source_period_end TEXT,
  capex_source TEXT,
  failure_reasons TEXT,
  PRIMARY KEY (asof_date, ticker)
);

CREATE TABLE IF NOT EXISTS jpx_regulation_flags(
  asof_date TEXT NOT NULL,
  source_name TEXT NOT NULL,
  ticker TEXT NOT NULL,
  flag TEXT NOT NULL,
  fetched_at_utc TEXT,
  PRIMARY KEY (asof_date, source_name, ticker, flag)
);

CREATE TABLE IF NOT EXISTS jpx_regulation_sources(
  asof_date TEXT NOT NULL,
  source_name TEXT NOT NULL,
  fetched_at_utc TEXT,
  PRIMARY KEY (asof_date, source_name)
);

CREATE TABLE IF NOT EXISTS source_coverage(
  source TEXT NOT NULL,
  coverage_key TEXT NOT NULL,
  coverage_start TEXT,
  coverage_end TEXT,
  fetched_at_utc TEXT NOT NULL,
  record_count INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok',
  error TEXT,
  PRIMARY KEY (source, coverage_key)
);

CREATE INDEX IF NOT EXISTS idx_source_coverage_source_window
  ON source_coverage(source, coverage_start, coverage_end);
"""


class SQLiteSchemaError(RuntimeError):
    """Raised when an existing SQLite cache is not the current schema."""


SQLiteCacheError = SQLiteSchemaError
def open_connection(db_path: Path) -> sqlite3.Connection:
    """Open the current SQLite cache, creating current tables on first use."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        tables = _existing_tables(conn)
        if tables:
            validate_current_schema(conn)
        else:
            conn.executescript(_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _validate_current_schema(conn: sqlite3.Connection, tables: set[str]) -> None:
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if user_version != SQLITE_SCHEMA_VERSION:
        raise SQLiteSchemaError(
            "unsupported screening SQLite schema; remove the SQLite file and rebuild it with "
            "`bootstrap-cache --asof` and `extract-edinet-metrics` "
            f"(found user_version={user_version}, expected={SQLITE_SCHEMA_VERSION})"
        )
    missing = sorted(set(_REQUIRED_TABLES) - tables)
    if missing:
        raise SQLiteSchemaError(
            "screening SQLite schema is incomplete; remove the SQLite file and rebuild it "
            f"(missing tables: {', '.join(missing)})"
        )
    expected_tables, expected_indexes = _expected_schema_shape()
    for table, required_columns in _REQUIRED_COLUMNS.items():
        existing_info = _table_info(conn, table)
        existing_columns = {column[0] for column in existing_info}
        missing_columns = sorted(set(required_columns) - existing_columns)
        if missing_columns:
            raise SQLiteSchemaError(
                "screening SQLite schema is incomplete; remove the SQLite file and rebuild it "
                f"(missing columns in {table}: {', '.join(missing_columns)})"
            )
        if existing_info != expected_tables[table]:
            raise SQLiteSchemaError(
                "screening SQLite schema is incomplete; remove the SQLite file and rebuild it "
                f"(table shape mismatch: {table})"
            )
    for index_name, expected_index in expected_indexes.items():
        existing_index = _index_info(conn, expected_index[0], index_name)
        if existing_index != expected_index:
            raise SQLiteSchemaError(
                "screening SQLite schema is incomplete; remove the SQLite file and rebuild it "
                f"(index shape mismatch: {index_name})"
            )


def validate_current_schema(conn: sqlite3.Connection) -> None:
    """Validate that an existing screening SQLite connection has the current schema."""
    _validate_current_schema(conn, _existing_tables(conn))


def _expected_schema_shape() -> tuple[
    dict[str, tuple[tuple[str, str, int, int], ...]],
    dict[str, tuple[str, tuple[str, ...], bool]],
]:
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(_SCHEMA_SQL)
        tables = {table: _table_info(expected, table) for table in _REQUIRED_TABLES}
        indexes: dict[str, tuple[str, tuple[str, ...], bool]] = {}
        for index_name, (table, _, _) in _EXPLICIT_INDEXES.items():
            index_info = _index_info(expected, table, index_name)
            if index_info is None:
                raise SQLiteSchemaError(f"internal schema definition is missing {index_name}")
            indexes[index_name] = index_info
        return tables, indexes
    finally:
        expected.close()


def _table_info(conn: sqlite3.Connection, table: str) -> tuple[tuple[str, str, int, int], ...]:
    rows = conn.execute('SELECT name, type, "notnull", pk FROM pragma_table_info(?)', (table,))
    return tuple(
        (str(name), str(column_type).upper(), int(notnull or 0), int(pk or 0))
        for name, column_type, notnull, pk in rows
    )


def _index_info(
    conn: sqlite3.Connection, table: str, index_name: str
) -> tuple[str, tuple[str, ...], bool] | None:
    rows = conn.execute(
        'SELECT name, "unique", origin FROM pragma_index_list(?)',
        (table,),
    ).fetchall()
    for name, unique, origin in rows:
        if str(name) != index_name or str(origin) != "c":
            continue
        column_rows = conn.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
            (index_name,),
        ).fetchall()
        return (table, tuple(str(row[0]) for row in column_rows), bool(unique))
    return None
