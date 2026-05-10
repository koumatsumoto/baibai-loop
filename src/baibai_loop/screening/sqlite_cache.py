"""SQLite canonical store for screening inputs.

SQLite is the local source of truth for screening inputs. Provider fetch paths
write normalized rows and source coverage directly into this database.
This module owns:

- the current-only SQLite schema (versioned via `PRAGMA user_version`)
- direct per-source upsert helpers used by providers
- `source_coverage`, which records request windows that must be present before
  `screening run` can execute
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .providers.jquants import (
    JQuantsProviderError,
    normalize_sector_name,
    parse_jquants_code,
)

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

_DELETE_DATE_RANGE_SQL = {
    ("jquants_daily_bars", "traded_at"): (
        "DELETE FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ?"
    ),
    ("jquants_fin_summaries", "disclosed_at"): (
        "DELETE FROM jquants_fin_summaries WHERE disclosed_at BETWEEN ? AND ?"
    ),
    ("jquants_market_calendar", "day"): (
        "DELETE FROM jquants_market_calendar WHERE day BETWEEN ? AND ?"
    ),
}
_COUNT_DATE_RANGE_SQL = {
    ("jquants_daily_bars", "traded_at"): (
        "SELECT COUNT(*) FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ?"
    ),
    ("jquants_fin_summaries", "disclosed_at"): (
        "SELECT COUNT(*) FROM jquants_fin_summaries WHERE disclosed_at BETWEEN ? AND ?"
    ),
    ("jquants_market_calendar", "day"): (
        "SELECT COUNT(*) FROM jquants_market_calendar WHERE day BETWEEN ? AND ?"
    ),
    ("edinet_documents", "doc_date"): (
        "SELECT COUNT(*) FROM edinet_documents WHERE doc_date BETWEEN ? AND ?"
    ),
    ("edinet_metrics", "asof_date"): (
        "SELECT COUNT(*) FROM edinet_metrics WHERE asof_date BETWEEN ? AND ?"
    ),
    ("jpx_regulation_flags", "asof_date"): (
        "SELECT COUNT(*) FROM jpx_regulation_flags WHERE asof_date BETWEEN ? AND ?"
    ),
}
_COUNT_TABLE_SQL = {
    "jquants_master_snapshots": "SELECT COUNT(*) FROM jquants_master_snapshots",
    "jquants_earnings_calendar": "SELECT COUNT(*) FROM jquants_earnings_calendar",
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


@dataclass(frozen=True, slots=True)
class _NormalizedRows:
    rows: list[tuple[Any, ...]]
    rejected_count: int = 0
    excluded_count: int = 0

    @property
    def skipped_count(self) -> int:
        return self.rejected_count + self.excluded_count

    @property
    def status(self) -> str:
        return "partial" if self.rejected_count else "ok"

    @property
    def error(self) -> str | None:
        if not self.rejected_count:
            return None
        return f"{self.rejected_count} rejected records during normalization"


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


def _delete_source_coverage(conn: sqlite3.Connection, source: str) -> None:
    conn.execute("DELETE FROM source_coverage WHERE source = ?", (source,))


def _delete_overlapping_source_coverage(
    conn: sqlite3.Connection,
    source: str,
    start: date,
    end: date,
) -> None:
    conn.execute(
        "DELETE FROM source_coverage WHERE source = ? "
        "AND coverage_start IS NOT NULL AND coverage_end IS NOT NULL "
        "AND coverage_start <= ? AND coverage_end >= ?",
        (source, end.isoformat(), start.isoformat()),
    )


def _delete_date_range(
    conn: sqlite3.Connection,
    table: str,
    date_column: str,
    start: date,
    end: date,
) -> None:
    conn.execute(
        _DELETE_DATE_RANGE_SQL[(table, date_column)],
        (start.isoformat(), end.isoformat()),
    )


def _date_range_row_count(
    conn: sqlite3.Connection,
    table: str,
    date_column: str,
    start: date,
    end: date,
) -> int:
    row = conn.execute(
        _COUNT_DATE_RANGE_SQL[(table, date_column)],
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return int(row[0] or 0)


def _table_row_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(_COUNT_TABLE_SQL[table]).fetchone()
    return int(row[0] or 0)


def store_jquants_daily_bars(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_start: date,
    requested_end: date,
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        normalized = _bars_rows_with_quality(records_list)
        rows = normalized.rows
        _delete_date_range(conn, "jquants_daily_bars", "traded_at", requested_start, requested_end)
        _delete_overlapping_source_coverage(
            conn, "jquants_daily_bars", requested_start, requested_end
        )
        if rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO jquants_daily_bars(
                  ticker, traded_at, open, high, low, close, volume, turnover_value,
                  adjustment_open, adjustment_high, adjustment_low, adjustment_close,
                  adjustment_volume, adjustment_factor, upper_limit, lower_limit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        persisted_count = _date_range_row_count(
            conn, "jquants_daily_bars", "traded_at", requested_start, requested_end
        )
        _record_source_coverage(
            conn,
            source="jquants_daily_bars",
            operation="get_eq_bars_daily_range",
            coverage_key=_range_coverage_key(
                "get_eq_bars_daily_range", requested_start, requested_end
            ),
            coverage_start=requested_start.isoformat(),
            coverage_end=requested_end.isoformat(),
            requested_start=requested_start.isoformat(),
            requested_end=requested_end.isoformat(),
            params={"start_dt": requested_start.isoformat(), "end_dt": requested_end.isoformat()},
            record_count=persisted_count,
            raw_record_count=len(records_list),
            skipped_record_count=normalized.skipped_count,
            rejected_record_count=normalized.rejected_count,
            excluded_record_count=normalized.excluded_count,
            status=normalized.status,
            error=normalized.error,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def store_jquants_fin_summaries(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_start: date,
    requested_end: date,
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        normalized = _fin_summary_rows_with_quality(records_list)
        rows = normalized.rows
        _delete_date_range(
            conn, "jquants_fin_summaries", "disclosed_at", requested_start, requested_end
        )
        _delete_overlapping_source_coverage(
            conn, "jquants_fin_summaries", requested_start, requested_end
        )
        if rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO jquants_fin_summaries(
                  ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding,
                  sales, cfo, cash_eq, total_assets, equity, operating_profit, ordinary_profit,
                  profit, fiscal_period, fiscal_year_end, period_start, period_end
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        persisted_count = _date_range_row_count(
            conn, "jquants_fin_summaries", "disclosed_at", requested_start, requested_end
        )
        _record_source_coverage(
            conn,
            source="jquants_fin_summaries",
            operation="get_fin_summary_range",
            coverage_key=_range_coverage_key(
                "get_fin_summary_range", requested_start, requested_end
            ),
            coverage_start=requested_start.isoformat(),
            coverage_end=requested_end.isoformat(),
            requested_start=requested_start.isoformat(),
            requested_end=requested_end.isoformat(),
            params={"start_dt": requested_start.isoformat(), "end_dt": requested_end.isoformat()},
            record_count=persisted_count,
            raw_record_count=len(records_list),
            skipped_record_count=normalized.skipped_count,
            rejected_record_count=normalized.rejected_count,
            excluded_record_count=normalized.excluded_count,
            status=normalized.status,
            error=normalized.error,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def store_jquants_master(db_path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        normalized = _master_rows_with_quality(records_list)
        rows = normalized.rows
        conn.execute("DELETE FROM jquants_master_snapshots")
        _delete_source_coverage(conn, "jquants_master_snapshots")
        if rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO jquants_master_snapshots(
                  snapshot_date, ticker, name, market, sector_33, is_common_stock
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        persisted_count = _table_row_count(conn, "jquants_master_snapshots")
        dates = sorted({row[0] for row in rows if row[0] != "unknown"})
        _record_source_coverage(
            conn,
            source="jquants_master_snapshots",
            operation="get_eq_master",
            coverage_key="latest",
            coverage_start=dates[0] if dates else None,
            coverage_end=dates[-1] if dates else None,
            requested_start=None,
            requested_end=None,
            params={},
            record_count=persisted_count,
            raw_record_count=len(records_list),
            skipped_record_count=normalized.skipped_count,
            rejected_record_count=normalized.rejected_count,
            excluded_record_count=normalized.excluded_count,
            status=normalized.status,
            error=normalized.error,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def store_jquants_earnings_calendar(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_start: date | None = None,
    requested_end: date | None = None,
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        normalized = _earnings_calendar_rows_with_quality(records_list)
        rows = normalized.rows
        conn.execute("DELETE FROM jquants_earnings_calendar")
        _delete_source_coverage(conn, "jquants_earnings_calendar")
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jquants_earnings_calendar("
                "announcement_date, ticker"
                ") VALUES (?, ?)",
                rows,
            )
        persisted_count = _table_row_count(conn, "jquants_earnings_calendar")
        dates = sorted({row[0] for row in rows})
        coverage_start = (
            requested_start.isoformat() if requested_start else dates[0] if dates else None
        )
        coverage_end = requested_end.isoformat() if requested_end else dates[-1] if dates else None
        params = {
            key: value.isoformat()
            for key, value in (("start_dt", requested_start), ("end_dt", requested_end))
            if value is not None
        }
        _record_source_coverage(
            conn,
            source="jquants_earnings_calendar",
            operation="get_eq_earnings_cal",
            coverage_key="whole-list",
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            requested_start=requested_start.isoformat() if requested_start else None,
            requested_end=requested_end.isoformat() if requested_end else None,
            params=params,
            record_count=persisted_count,
            raw_record_count=len(records_list),
            skipped_record_count=normalized.skipped_count,
            rejected_record_count=normalized.rejected_count,
            excluded_record_count=normalized.excluded_count,
            status=normalized.status,
            error=normalized.error,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def store_jquants_market_calendar(
    db_path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    requested_start: date,
    requested_end: date,
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        normalized = _market_calendar_rows_with_quality(records_list)
        rows = normalized.rows
        _delete_date_range(conn, "jquants_market_calendar", "day", requested_start, requested_end)
        _delete_overlapping_source_coverage(
            conn, "jquants_market_calendar", requested_start, requested_end
        )
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jquants_market_calendar(day, is_business_day) "
                "VALUES (?, ?)",
                rows,
            )
        persisted_count = _date_range_row_count(
            conn, "jquants_market_calendar", "day", requested_start, requested_end
        )
        _record_source_coverage(
            conn,
            source="jquants_market_calendar",
            operation="get_mkt_calendar",
            coverage_key=_range_coverage_key("get_mkt_calendar", requested_start, requested_end),
            coverage_start=requested_start.isoformat(),
            coverage_end=requested_end.isoformat(),
            requested_start=requested_start.isoformat(),
            requested_end=requested_end.isoformat(),
            params={
                "from_yyyymmdd": requested_start.strftime("%Y%m%d"),
                "to_yyyymmdd": requested_end.strftime("%Y%m%d"),
            },
            record_count=persisted_count,
            raw_record_count=len(records_list),
            skipped_record_count=normalized.skipped_count,
            rejected_record_count=normalized.rejected_count,
            excluded_record_count=normalized.excluded_count,
            status=normalized.status,
            error=normalized.error,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def store_edinet_documents(
    db_path: Path,
    on_date: date,
    records: Iterable[Mapping[str, Any]],
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        rows = _edinet_document_rows(on_date.isoformat(), records_list)
        conn.execute("DELETE FROM edinet_documents WHERE doc_date = ?", (on_date.isoformat(),))
        _delete_overlapping_source_coverage(conn, "edinet_documents", on_date, on_date)
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO edinet_documents("
                "doc_date, doc_id, sec_code, doc_type_code, csv_flag, xbrl_flag, "
                "legal_status, disclosure_status, withdrawal_status, submit_datetime, "
                "doc_description, period_start, period_end"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        persisted_count = _date_range_row_count(
            conn, "edinet_documents", "doc_date", on_date, on_date
        )
        _record_source_coverage(
            conn,
            source="edinet_documents",
            operation="documents",
            coverage_key=on_date.isoformat(),
            coverage_start=on_date.isoformat(),
            coverage_end=on_date.isoformat(),
            requested_start=on_date.isoformat(),
            requested_end=on_date.isoformat(),
            params={"date": on_date.isoformat(), "type": 2},
            record_count=persisted_count,
            raw_record_count=len(records_list),
            skipped_record_count=len(records_list) - len(rows),
            status="partial" if len(rows) < len(records_list) else "ok",
            error=(
                f"{len(records_list) - len(rows)} EDINET document rows were skipped"
                if len(rows) < len(records_list)
                else None
            ),
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def store_edinet_metrics(
    db_path: Path,
    asof_date: date,
    records: Iterable[Mapping[str, Any]],
    *,
    status: str = "ok",
    error: str | None = None,
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        rows = _edinet_metric_rows(asof_date.isoformat(), records_list)
        skipped_count = len(records_list) - len(rows)
        stored_status = status
        stored_error = error
        if status == "ok" and skipped_count:
            stored_status = "partial"
            stored_error = f"{skipped_count} EDINET metric rows were skipped"
        conn.execute("DELETE FROM edinet_metrics WHERE asof_date = ?", (asof_date.isoformat(),))
        _delete_overlapping_source_coverage(conn, "edinet_metrics", asof_date, asof_date)
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO edinet_metrics("
                "asof_date, ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
                "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, "
                "ttm_quality_pcfr, operating_profit_ttm, depreciation_and_amortization_ttm, "
                "capex_ttm, fcf_ttm, net_cash, equity, total_assets, ttm_quality_fcf, "
                "ttm_quality_net_cash, source_doc_id, document_type, source_submit_datetime, "
                "source_period_start, source_period_end, capex_source, failure_reasons"
                ") VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
                ")",
                rows,
            )
        persisted_count = _date_range_row_count(
            conn, "edinet_metrics", "asof_date", asof_date, asof_date
        )
        _record_source_coverage(
            conn,
            source="edinet_metrics",
            operation="metrics",
            coverage_key=asof_date.isoformat(),
            coverage_start=asof_date.isoformat(),
            coverage_end=asof_date.isoformat(),
            requested_start=asof_date.isoformat(),
            requested_end=asof_date.isoformat(),
            params={"asof_date": asof_date.isoformat()},
            record_count=persisted_count,
            raw_record_count=len(records_list),
            skipped_record_count=skipped_count,
            status=stored_status,
            error=stored_error,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def store_jpx_regulations(
    db_path: Path,
    asof_date: date,
    *,
    flags_by_ticker: Mapping[str, Iterable[str]],
    source_names: Iterable[str],
    fetched_at_utc: str | None = None,
) -> int:
    conn = open_connection(db_path)
    fetched_at = fetched_at_utc or datetime.now(UTC).isoformat()
    source_name_tuple = tuple(source_names)
    try:
        conn.execute(
            "DELETE FROM jpx_regulation_flags WHERE asof_date = ?", (asof_date.isoformat(),)
        )
        conn.execute(
            "DELETE FROM jpx_regulation_sources WHERE asof_date = ?",
            (asof_date.isoformat(),),
        )
        _delete_overlapping_source_coverage(conn, "jpx_regulation_flags", asof_date, asof_date)
        source_rows = [(asof_date.isoformat(), str(name), fetched_at) for name in source_name_tuple]
        if source_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jpx_regulation_sources("
                "asof_date, source_name, fetched_at_utc"
                ") VALUES (?, ?, ?)",
                source_rows,
            )
        rows: list[tuple[Any, ...]] = []
        raw_record_count = 0
        rejected_count = 0
        for raw_ticker, flags in flags_by_ticker.items():
            raw_record_count += 1
            ticker = _normalize_ticker_or_none(raw_ticker)
            if ticker is None:
                rejected_count += 1
                continue
            accepted_for_ticker = 0
            rejected_for_ticker = 0
            for flag in flags:
                flag_text = _to_str_or_none(flag)
                if flag_text is None:
                    rejected_for_ticker += 1
                    continue
                rows.append((asof_date.isoformat(), flag_text, ticker, flag_text, fetched_at))
                accepted_for_ticker += 1
            rejected_count += rejected_for_ticker
            if accepted_for_ticker == 0 and rejected_for_ticker == 0:
                rejected_count += 1
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jpx_regulation_flags("
                "asof_date, source_name, ticker, flag, fetched_at_utc"
                ") VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        persisted_count = _date_range_row_count(
            conn, "jpx_regulation_flags", "asof_date", asof_date, asof_date
        )
        _record_source_coverage(
            conn,
            source="jpx_regulation_flags",
            operation="regulations",
            coverage_key=asof_date.isoformat(),
            coverage_start=asof_date.isoformat(),
            coverage_end=asof_date.isoformat(),
            requested_start=asof_date.isoformat(),
            requested_end=asof_date.isoformat(),
            params={"asof_date": asof_date.isoformat(), "source_names": sorted(source_name_tuple)},
            record_count=persisted_count,
            raw_record_count=raw_record_count,
            skipped_record_count=0,
            rejected_record_count=rejected_count,
            status="partial" if rejected_count else "ok",
            error=(
                f"{rejected_count} JPX regulation records were rejected" if rejected_count else None
            ),
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def _code_quality(value: Any) -> tuple[str | None, str]:
    if value in (None, ""):
        return None, "rejected"
    try:
        ticker, common_code = _parse_with_common_flag(value)
    except JQuantsProviderError:
        return None, "rejected"
    if not common_code:
        return None, "excluded"
    return ticker, "ok"


def _earnings_calendar_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> _NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, code_status = _code_quality(_first(record, "Code", "code"))
        if code_status == "rejected":
            rejected_count += 1
            continue
        if code_status == "excluded":
            excluded_count += 1
            continue
        announcement_date = _date_iso(
            _first(record, "Date", "date", "AnnouncementDate", "announcement_date")
        )
        if ticker is None or announcement_date is None:
            rejected_count += 1
            continue
        rows.append(
            (
                announcement_date,
                ticker,
            )
        )
    return _NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


def _earnings_calendar_rows(records: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return _earnings_calendar_rows_with_quality(records).rows


def _market_calendar_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> _NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    for record in records:
        day = _date_iso(_first(record, "Date", "date"))
        if day is None:
            rejected_count += 1
            continue
        # Mirror JQuantsProvider: HolidayDivision "1" (営業日) and "2"
        # (半日営業: 大納会など) both count as business days.
        division = _to_str_or_none(
            _first(record, "HolidayDivision", "holiday_division", "HolDiv", "hol_div")
        )
        is_business_day = 1 if division in {"1", "2"} else 0
        rows.append(
            (
                day,
                is_business_day,
            )
        )
    return _NormalizedRows(rows=rows, rejected_count=rejected_count)


def _market_calendar_rows(records: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return _market_calendar_rows_with_quality(records).rows


def _edinet_document_rows(
    doc_date: str,
    records: Iterable[Mapping[str, Any]],
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for record in records:
        doc_id = _to_str_or_none(_first(record, "docID", "doc_id"))
        if doc_id is None:
            continue
        rows.append(
            (
                doc_date,
                doc_id,
                _to_str_or_none(_first(record, "secCode", "sec_code")),
                _to_str_or_none(_first(record, "docTypeCode", "doc_type_code")),
                _to_str_or_none(_first(record, "csvFlag", "csv_flag")),
                _to_str_or_none(_first(record, "xbrlFlag", "xbrl_flag")),
                _to_str_or_none(_first(record, "legalStatus", "legal_status")),
                _to_str_or_none(_first(record, "disclosureStatus", "disclosure_status")),
                _to_str_or_none(_first(record, "withdrawalStatus", "withdrawal_status")),
                _to_str_or_none(_first(record, "submitDateTime", "submit_datetime")),
                _to_str_or_none(_first(record, "docDescription", "doc_description")),
                _date_iso(_first(record, "periodStart", "period_start")),
                _date_iso(_first(record, "periodEnd", "period_end")),
            )
        )
    return rows


def _edinet_metric_rows(
    asof_date: str,
    records: Iterable[Mapping[str, Any]],
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for record in records:
        ticker = _normalize_ticker_or_none(_first(record, "secCode", "ticker", "code", "Code"))
        if ticker is None:
            continue
        rows.append(
            (
                asof_date,
                ticker,
                _to_float(_first(record, "sales_ttm", "SalesTTM")),
                _to_float(_first(record, "ocf_ttm", "OperatingCashFlowTTM")),
                _to_float(_first(record, "debt", "Debt")),
                _to_float(_first(record, "cash", "Cash")),
                _to_float(_first(record, "ebitda_ttm", "EBITDATTM")),
                _to_str_or_none(_first(record, "consolidation_basis", "ConsolidationBasis")),
                _to_str_or_none(_first(record, "ttm_quality_ev_ebitda", "TTMQualityEvEbitda")),
                _to_str_or_none(_first(record, "ttm_quality_p_s", "TTMQualityPS")),
                _to_str_or_none(_first(record, "ttm_quality_pcfr", "TTMQualityPCFR")),
                _to_float(_first(record, "operating_profit_ttm")),
                _to_float(_first(record, "depreciation_and_amortization_ttm")),
                _to_float(_first(record, "capex_ttm")),
                _to_float(_first(record, "fcf_ttm")),
                _to_float(_first(record, "net_cash")),
                _to_float(_first(record, "equity")),
                _to_float(_first(record, "total_assets")),
                _to_str_or_none(_first(record, "ttm_quality_fcf")),
                _to_str_or_none(_first(record, "ttm_quality_net_cash")),
                _to_str_or_none(_first(record, "source_doc_id")),
                _to_str_or_none(_first(record, "document_type")),
                _to_str_or_none(_first(record, "source_submit_datetime")),
                _to_str_or_none(_first(record, "source_period_start")),
                _to_str_or_none(_first(record, "source_period_end")),
                _to_str_or_none(_first(record, "capex_source")),
                json.dumps(_first(record, "failure_reasons") or (), ensure_ascii=False),
            )
        )
    return rows


def _bars_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> _NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, code_status = _code_quality(_first(record, "Code", "code"))
        if code_status == "rejected":
            rejected_count += 1
            continue
        if code_status == "excluded":
            excluded_count += 1
            continue
        traded_at = _date_iso(_first(record, "Date", "date", "TradedAt", "traded_at"))
        if ticker is None or traded_at is None:
            rejected_count += 1
            continue
        rows.append(
            (
                ticker,
                traded_at,
                _to_float(_first(record, "Open", "open", "O", "o")),
                _to_float(_first(record, "High", "high", "H", "h")),
                _to_float(_first(record, "Low", "low", "L", "l")),
                _to_float(_first(record, "Close", "close", "C", "c")),
                _to_float(_first(record, "Volume", "volume", "Vo", "vo")),
                _to_float(_first(record, "TurnoverValue", "turnover_value", "Va", "va")),
                _to_float(_first(record, "AdjustmentOpen", "adjustment_open", "AdjO", "adj_o")),
                _to_float(_first(record, "AdjustmentHigh", "adjustment_high", "AdjH", "adj_h")),
                _to_float(_first(record, "AdjustmentLow", "adjustment_low", "AdjL", "adj_l")),
                _to_float(_first(record, "AdjustmentClose", "adjustment_close", "AdjC", "adj_c")),
                _to_float(
                    _first(record, "AdjustmentVolume", "adjustment_volume", "AdjVo", "adj_vo")
                ),
                _to_float(_first(record, "AdjustmentFactor", "adjustment_factor", "AdjFactor")),
                _to_str_or_none(_first(record, "UpperLimit", "upper_limit", "UL")),
                _to_str_or_none(_first(record, "LowerLimit", "lower_limit", "LL")),
            )
        )
    return _NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


def _fin_summary_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> _NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, quality = _code_quality(_first(record, "Code", "code"))
        if quality == "rejected":
            rejected_count += 1
            continue
        if quality == "excluded":
            excluded_count += 1
            continue
        disclosed_at = _date_iso(
            _first(record, "DisclosedDate", "disclosed_at", "DiscDate", "disc_date", "Date")
        )
        if ticker is None or disclosed_at is None:
            rejected_count += 1
            continue
        rows.append(
            (
                ticker,
                disclosed_at,
                _to_float(_first(record, "ForecastEPS", "forecast_eps", "FEPS")),
                _to_float(_first(record, "EpsTtm", "eps_ttm", "EPS", "eps")),
                _to_float(_first(record, "BPS", "bps")),
                _to_float(
                    _first(
                        record,
                        "SharesOutstanding",
                        "shares_outstanding",
                        "IssuedShareEquityQuote",
                        "ShOutFY",
                        "AvgSh",
                    )
                ),
                _to_float(_first(record, "NetSales", "net_sales", "Sales", "sales")),
                _to_float(
                    _first(
                        record,
                        "CashFlowsFromOperatingActivities",
                        "cash_flows_from_operating_activities",
                        "OperatingCashFlow",
                        "operating_cash_flow",
                        "CFO",
                        "cfo",
                    )
                ),
                _to_float(
                    _first(
                        record,
                        "CashAndEquivalents",
                        "cash_and_equivalents",
                        "CashEq",
                        "cash_eq",
                    )
                ),
                _to_float(_first(record, "TotalAssets", "total_assets", "TA", "ta")),
                _to_float(_first(record, "Equity", "equity", "Eq", "eq")),
                _to_float(_first(record, "OperatingProfit", "operating_profit", "OP")),
                _to_float(_first(record, "OrdinaryProfit", "ordinary_profit", "OdP")),
                _to_float(_first(record, "Profit", "profit", "NP")),
                _to_str_or_none(
                    _first(record, "TypeOfCurrentPeriod", "type_of_current_period", "CurPerType")
                ),
                _date_iso(
                    _first(
                        record,
                        "CurrentFiscalYearEndDate",
                        "current_fiscal_year_end_date",
                        "CurFYEn",
                    )
                ),
                _date_iso(
                    _first(
                        record, "CurrentPeriodStartDate", "current_period_start_date", "CurPerSt"
                    )
                ),
                _date_iso(
                    _first(record, "CurrentPeriodEndDate", "current_period_end_date", "CurPerEn")
                ),
            )
        )
    return _NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


def _master_rows_with_quality(records: Iterable[Mapping[str, Any]]) -> _NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, code_status = _code_quality(_first(record, "Code", "code"))
        if code_status == "rejected":
            rejected_count += 1
            continue
        if code_status == "excluded":
            excluded_count += 1
            continue
        snapshot_date = _date_iso(_first(record, "Date", "date", "snapshot_date")) or "unknown"
        is_common_stock = _is_common_stock_flag(record)
        sector_raw = _to_str_or_none(
            _first(record, "Sector33CodeName", "sector_33", "Sector33Name", "S33Nm", "S33")
        )
        rows.append(
            (
                snapshot_date,
                ticker,
                _to_str_or_none(_first(record, "CompanyName", "company_name", "Name", "CoName")),
                _to_str_or_none(_first(record, "MarketCodeName", "market_segment", "MktNm", "Mkt")),
                # J-Quants は同じ TSE 33 セクターを半角中黒 (U+FF65)・全角中黒 (U+30FB) で
                # 揺らせて返してくる。SQLite に取り込む段階で全角形に正規化し、outlook /
                # candidates / select の matcher が一意に解決できるようにする。
                normalize_sector_name(sector_raw) if sector_raw else sector_raw,
                1 if is_common_stock else 0,
            )
        )
    return _NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


def _record_source_coverage(
    conn: sqlite3.Connection,
    *,
    source: str,
    coverage_key: str,
    coverage_start: str | None,
    coverage_end: str | None,
    record_count: int,
    status: str = "ok",
    error: str | None = None,
    fetched_at_utc: str | None = None,
    replace: bool = True,
    **_: Any,
) -> None:
    """Record the minimal provider coverage needed for preflight checks."""
    conflict_action = "REPLACE" if replace else "IGNORE"
    conn.execute(
        f"""
        INSERT OR {conflict_action} INTO source_coverage(
          source, coverage_key, coverage_start, coverage_end,
          fetched_at_utc, record_count, status, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            coverage_key,
            coverage_start,
            coverage_end,
            fetched_at_utc or datetime.now(UTC).isoformat(),
            record_count,
            status,
            error,
        ),
    )


def _range_coverage_key(operation: str, start: date, end: date) -> str:
    return f"{operation}:{start.isoformat()}..{end.isoformat()}"


def _normalize_ticker_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        ticker, common_code = _parse_with_common_flag(value)
    except JQuantsProviderError:
        return None
    if not common_code:
        return None
    return ticker


def _parse_with_common_flag(value: Any) -> tuple[str, bool]:
    """Mirror JQuantsProvider's 5-char common-code suffix handling."""
    raw = str(value or "").strip().upper()
    if len(raw) == 4:
        return parse_jquants_code(raw), True
    if len(raw) == 5 and raw[:4].isalnum():
        return parse_jquants_code(raw), raw.endswith("0")
    raise JQuantsProviderError(f"invalid J-Quants code: {value!r}")


def _is_common_stock_flag(record: Mapping[str, Any]) -> bool:
    if "is_common_stock" in record:
        return bool(record["is_common_stock"])
    code = str(_first(record, "Code", "code") or "").strip()
    if len(code) == 5 and not code.endswith("0"):
        return False
    raw_type = _to_str_or_none(_first(record, "TypeOfDocument", "SecurityType", "security_type"))
    if raw_type is None:
        return True
    return raw_type.lower() in {"common", "common stock", "普通株"}


def _first(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _to_float(value: Any) -> float | None:
    if value in (None, "", "-", "null"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


def _to_str_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _date_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text[:10]
