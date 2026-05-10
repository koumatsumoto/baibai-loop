"""SQLite canonical store for screening inputs.

SQLite is the local source of truth for screening inputs. Provider fetch paths
write normalized rows and source coverage directly into this database; legacy
raw JSON import remains only as a migration/backfill command.
This module owns:

- the SQLite schema (versioned via `SCHEMA_VERSION`)
- direct per-source upsert helpers used by providers
- `source_coverage`, which records request windows that must be present before
  `screening run` can execute
- legacy `rebuild_from_raw()` for one-time import from old raw JSON trees
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .providers.jquants import (
    JQuantsProviderError,
    normalize_sector_name,
    parse_jquants_code,
)

SCHEMA_VERSION = "v9"

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
    "raw_imports",
    "cache_metadata",
)

_DATA_TABLES = tuple(
    table
    for table in _REQUIRED_TABLES
    if table not in {"source_coverage", "raw_imports", "cache_metadata"}
)
_TABLE_HAS_ROWS_SQL = {
    "jquants_daily_bars": "SELECT 1 FROM jquants_daily_bars LIMIT 1",
    "jquants_fin_summaries": "SELECT 1 FROM jquants_fin_summaries LIMIT 1",
    "jquants_master_snapshots": "SELECT 1 FROM jquants_master_snapshots LIMIT 1",
    "jquants_earnings_calendar": "SELECT 1 FROM jquants_earnings_calendar LIMIT 1",
    "jquants_market_calendar": "SELECT 1 FROM jquants_market_calendar LIMIT 1",
    "edinet_documents": "SELECT 1 FROM edinet_documents LIMIT 1",
    "edinet_metrics": "SELECT 1 FROM edinet_metrics LIMIT 1",
    "jpx_regulation_flags": "SELECT 1 FROM jpx_regulation_flags LIMIT 1",
    "jpx_regulation_sources": "SELECT 1 FROM jpx_regulation_sources LIMIT 1",
}
_TABLE_COUNT_SQL = {
    "jquants_daily_bars": "SELECT COUNT(*) FROM jquants_daily_bars",
    "jquants_fin_summaries": "SELECT COUNT(*) FROM jquants_fin_summaries",
    "jquants_master_snapshots": "SELECT COUNT(*) FROM jquants_master_snapshots",
    "jquants_earnings_calendar": "SELECT COUNT(*) FROM jquants_earnings_calendar",
    "jquants_market_calendar": "SELECT COUNT(*) FROM jquants_market_calendar",
    "edinet_documents": "SELECT COUNT(*) FROM edinet_documents",
    "edinet_metrics": "SELECT COUNT(*) FROM edinet_metrics",
    "jpx_regulation_flags": "SELECT COUNT(*) FROM jpx_regulation_flags",
    "jpx_regulation_sources": "SELECT COUNT(*) FROM jpx_regulation_sources",
}
_SINGLE_SNAPSHOT_SOURCES = frozenset(
    {
        "jquants_master_snapshots",
        "jquants_earnings_calendar",
    }
)
_RAW_IMPORT_COVERAGE_MIGRATION_KEY = "source_coverage_raw_import_migration"

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
  raw_json TEXT NOT NULL,
  PRIMARY KEY (ticker, disclosed_at)
);

CREATE TABLE IF NOT EXISTS jquants_master_snapshots(
  snapshot_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  name TEXT,
  market TEXT,
  sector_33 TEXT,
  is_common_stock INTEGER,
  raw_json TEXT NOT NULL,
  PRIMARY KEY (snapshot_date, ticker)
);

CREATE TABLE IF NOT EXISTS jquants_earnings_calendar(
  announcement_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  PRIMARY KEY (announcement_date, ticker)
);

CREATE TABLE IF NOT EXISTS jquants_market_calendar(
  day TEXT PRIMARY KEY,
  is_business_day INTEGER NOT NULL,
  raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edinet_documents(
  doc_date TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  sec_code TEXT,
  doc_type_code TEXT,
  raw_json TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS raw_imports(
  source TEXT NOT NULL,
  path TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  imported_at_utc TEXT NOT NULL,
  record_count INTEGER NOT NULL,
  min_date TEXT,
  max_date TEXT
);

CREATE TABLE IF NOT EXISTS source_coverage(
  source TEXT NOT NULL,
  operation TEXT NOT NULL,
  coverage_key TEXT NOT NULL,
  coverage_start TEXT,
  coverage_end TEXT,
  requested_start TEXT,
  requested_end TEXT,
  params_json TEXT NOT NULL,
  fetched_at_utc TEXT NOT NULL,
  record_count INTEGER NOT NULL,
  raw_record_count INTEGER,
  normalized_record_count INTEGER,
  skipped_record_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ok',
  error TEXT,
  PRIMARY KEY (source, operation, coverage_key)
);

CREATE INDEX IF NOT EXISTS idx_source_coverage_source_window
  ON source_coverage(source, coverage_start, coverage_end);

CREATE TABLE IF NOT EXISTS cache_metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class SQLiteCacheError(RuntimeError):
    """Raised when a raw JSON file cannot be imported into SQLite."""


@dataclass(frozen=True, slots=True)
class RebuildSummary:
    daily_bars_files: int
    daily_bars_rows: int
    fin_summary_files: int
    fin_summary_rows: int
    master_files: int
    master_rows: int
    earnings_calendar_files: int
    earnings_calendar_rows: int
    market_calendar_files: int
    market_calendar_rows: int
    edinet_document_files: int
    edinet_document_rows: int
    edinet_metric_files: int
    edinet_metric_rows: int
    jpx_regulation_files: int
    jpx_regulation_rows: int
    skipped_files: tuple[str, ...]


def open_connection(db_path: Path) -> sqlite3.Connection:
    """Open the SQLite cache, creating tables on first use."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA_SQL)
    _ensure_source_coverage_columns(conn)
    _migrate_source_coverage_from_raw_imports(conn)
    conn.execute(
        "INSERT OR REPLACE INTO cache_metadata(key, value) VALUES('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    return conn


def ensure_sqlite_schema(db_path: Path) -> None:
    """Create or migrate the SQLite schema without importing provider data."""
    conn = open_connection(db_path)
    conn.close()


def _ensure_source_coverage_columns(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(source_coverage)").fetchall()}
    migrations = {
        "raw_record_count": "ALTER TABLE source_coverage ADD COLUMN raw_record_count INTEGER",
        "normalized_record_count": (
            "ALTER TABLE source_coverage ADD COLUMN normalized_record_count INTEGER"
        ),
        "skipped_record_count": (
            "ALTER TABLE source_coverage ADD COLUMN skipped_record_count INTEGER NOT NULL DEFAULT 0"
        ),
        "status": "ALTER TABLE source_coverage ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'",
        "error": "ALTER TABLE source_coverage ADD COLUMN error TEXT",
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)


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
        f"DELETE FROM {table} WHERE {date_column} BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    )


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
        rows = list(_iter_bars_rows(records_list))
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
            record_count=len(rows),
            raw_record_count=len(records_list),
            skipped_record_count=len(records_list) - len(rows),
        )
        _record_table_integrity(conn)
        conn.commit()
        return len(rows)
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
        rows = list(_iter_fin_summary_rows(records_list))
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
                  profit, fiscal_period, fiscal_year_end, period_start, period_end, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
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
            record_count=len(rows),
            raw_record_count=len(records_list),
            skipped_record_count=len(records_list) - len(rows),
        )
        _record_table_integrity(conn)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def store_jquants_master(db_path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        rows = list(_iter_master_rows(records_list))
        conn.execute("DELETE FROM jquants_master_snapshots")
        _delete_source_coverage(conn, "jquants_master_snapshots")
        if rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO jquants_master_snapshots(
                  snapshot_date, ticker, name, market, sector_33, is_common_stock, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
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
            record_count=len(rows),
            raw_record_count=len(records_list),
            skipped_record_count=len(records_list) - len(rows),
        )
        _record_table_integrity(conn)
        conn.commit()
        return len(rows)
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
        rows = _earnings_calendar_rows(records_list)
        conn.execute("DELETE FROM jquants_earnings_calendar")
        _delete_source_coverage(conn, "jquants_earnings_calendar")
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jquants_earnings_calendar("
                "announcement_date, ticker, raw_json"
                ") VALUES (?, ?, ?)",
                rows,
            )
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
            record_count=len(rows),
            raw_record_count=len(records_list),
            skipped_record_count=len(records_list) - len(rows),
        )
        _record_table_integrity(conn)
        conn.commit()
        return len(rows)
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
        rows = _market_calendar_rows(records_list)
        _delete_date_range(conn, "jquants_market_calendar", "day", requested_start, requested_end)
        _delete_overlapping_source_coverage(
            conn, "jquants_market_calendar", requested_start, requested_end
        )
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jquants_market_calendar(day, is_business_day, raw_json) "
                "VALUES (?, ?, ?)",
                rows,
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
            record_count=len(rows),
            raw_record_count=len(records_list),
            skipped_record_count=len(records_list) - len(rows),
        )
        _record_table_integrity(conn)
        conn.commit()
        return len(rows)
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
                "doc_date, doc_id, sec_code, doc_type_code, raw_json"
                ") VALUES (?, ?, ?, ?, ?)",
                rows,
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
            record_count=len(rows),
            raw_record_count=len(records_list),
            skipped_record_count=len(records_list) - len(rows),
        )
        _record_table_integrity(conn)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def store_edinet_metrics(
    db_path: Path,
    asof_date: date,
    records: Iterable[Mapping[str, Any]],
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        rows = _edinet_metric_rows(asof_date.isoformat(), records_list)
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
            record_count=len(rows),
            raw_record_count=len(records_list),
            skipped_record_count=len(records_list) - len(rows),
        )
        _record_table_integrity(conn)
        conn.commit()
        return len(rows)
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
        for raw_ticker, flags in flags_by_ticker.items():
            ticker = _normalize_ticker_or_none(raw_ticker)
            if ticker is None:
                continue
            for flag in flags:
                flag_text = _to_str_or_none(flag)
                if flag_text is None:
                    continue
                rows.append((asof_date.isoformat(), flag_text, ticker, flag_text, fetched_at))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jpx_regulation_flags("
                "asof_date, source_name, ticker, flag, fetched_at_utc"
                ") VALUES (?, ?, ?, ?, ?)",
                rows,
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
            record_count=len(rows),
            raw_record_count=sum(1 for _ in flags_by_ticker.items()),
            skipped_record_count=0,
        )
        _record_table_integrity(conn)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def rebuild_from_raw(raw_dir: Path | Iterable[Path], db_path: Path) -> RebuildSummary:
    """Rebuild the SQLite cache from scratch by walking each entry in
    `raw_dir` (a single Path or an iterable of Paths). Files are dispatched
    by their parent directory + filename pattern (see `_rebuild`).

    Multiple raw dirs are supported for one-time migration from old local or
    repository raw JSON trees. Chunk windows may differ between trees, so
    merging gives the SQLite reader broader coverage.

    The destination file is replaced atomically after a complete successful
    import, so a parse failure or interruption does not destroy the last-good
    derived cache.
    """
    raw_dirs: tuple[Path, ...] = (raw_dir,) if isinstance(raw_dir, Path) else tuple(raw_dir)
    importable_paths = tuple(
        path for current_raw_dir in raw_dirs for path in _iter_importable_raw_paths(current_raw_dir)
    )
    if not importable_paths:
        raise SQLiteCacheError(
            "no importable legacy raw JSON files found; pass --raw-dir explicitly "
            "or bootstrap SQLite from providers"
        )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{db_path.name}.",
        suffix=".tmp",
        dir=db_path.parent,
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
    conn = open_connection(temp_path)
    try:
        summary = _rebuild(conn, raw_dirs)
        _record_table_integrity(conn)
        conn.commit()
        conn.close()
        temp_path.replace(db_path)
        return summary
    except Exception:
        conn.close()
        temp_path.unlink(missing_ok=True)
        raise


def refresh_from_raw(raw_dir: Path | Iterable[Path], db_path: Path) -> RebuildSummary:
    """Refresh SQLite from raw JSON without reparsing files that are already
    imported.

    Raw JSON remains canonical. The common `run` path appends a handful of
    new cache chunks for a new as-of date, so importing just those files keeps
    SQLite range-aware without paying the cost of a full 600MB+ rebuild. If an
    existing raw file was edited or deleted, fall back to `rebuild_from_raw()`
    so stale rows cannot survive from the previous import.
    """
    raw_dirs: tuple[Path, ...] = (raw_dir,) if isinstance(raw_dir, Path) else tuple(raw_dir)
    if _sqlite_schema_is_stale(db_path):
        return rebuild_from_raw(raw_dirs, db_path)

    live_raw_files = _live_raw_files_by_path(raw_dirs)
    live_raw_paths = set(live_raw_files)

    conn = open_connection(db_path)
    try:
        imported = _raw_import_sha_by_path(conn)
        if any(not _path_is_under_any(path_text, raw_dirs) for path_text in imported):
            conn.close()
            return rebuild_from_raw(raw_dirs, db_path)
        if not set(imported).issubset(live_raw_paths):
            conn.close()
            return rebuild_from_raw(raw_dirs, db_path)
        for path_text, previous_sha in imported.items():
            if live_raw_files[path_text][1] != previous_sha:
                conn.close()
                return rebuild_from_raw(raw_dirs, db_path)

        new_path_keys = [
            path_key for path_key in sorted(live_raw_paths) if path_key not in imported
        ]
        if _new_raw_files_require_rebuild(conn, [live_raw_files[key][0] for key in new_path_keys]):
            conn.close()
            return rebuild_from_raw(raw_dirs, db_path)

        counters = _RebuildCounters()
        for path_key in new_path_keys:
            path = live_raw_files[path_key][0]
            _import_raw_file(conn, path, counters)
        _record_table_integrity(conn)
        conn.commit()
        return _summary_from_counters(counters)
    finally:
        with suppress(sqlite3.Error):
            conn.close()


def is_sqlite_stale(raw_dirs: Iterable[Path], db_path: Path) -> bool:
    """True when SQLite is missing or out of sync with canonical raw JSON.

    The check is content-based, not mtime-based: raw files may be edited by
    tools that preserve an old timestamp, and the derived rows must still be
    rebuilt. Only supported raw files are considered because unknown JSON
    endpoints are intentionally skipped by the SQLite cache.
    """
    raw_dirs_tuple = tuple(raw_dirs)
    if _sqlite_schema_is_stale(db_path):
        return True
    live_raw_files = _live_raw_files_by_path(raw_dirs_tuple)
    try:
        conn = sqlite3.connect(db_path)
        try:
            imported = _raw_import_sha_by_path(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return True
    if any(not _path_is_under_any(path_text, raw_dirs_tuple) for path_text in imported):
        return True
    if set(imported) != set(live_raw_files):
        return True
    return any(live_raw_files[path_text][1] != sha for path_text, sha in imported.items())


def _sqlite_schema_is_stale(db_path: Path) -> bool:
    if not db_path.exists():
        return True
    try:
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if any(table not in tables for table in _REQUIRED_TABLES):
                return True
            row = conn.execute(
                "SELECT value FROM cache_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                return True
            import_count = conn.execute("SELECT COUNT(*) FROM raw_imports").fetchone()[0]
            if import_count == 0:
                for table in _DATA_TABLES:
                    if conn.execute(_TABLE_HAS_ROWS_SQL[table]).fetchone() is not None:
                        return True
                return False
            if _table_integrity_is_stale(conn):
                return True
        finally:
            conn.close()
    except sqlite3.Error:
        return True
    return False


@dataclass
class _RebuildCounters:
    bars_files: int = 0
    bars_rows: int = 0
    fin_files: int = 0
    fin_rows: int = 0
    master_files: int = 0
    master_rows: int = 0
    earnings_files: int = 0
    earnings_rows: int = 0
    market_calendar_files: int = 0
    market_calendar_rows: int = 0
    edinet_doc_files: int = 0
    edinet_doc_rows: int = 0
    edinet_metric_files: int = 0
    edinet_metric_rows: int = 0
    jpx_reg_files: int = 0
    jpx_reg_rows: int = 0
    skipped: list[str] = field(default_factory=list)


def _rebuild(conn: sqlite3.Connection, raw_dirs: tuple[Path, ...]) -> RebuildSummary:
    counters = _RebuildCounters()
    for raw_dir in raw_dirs:
        if not raw_dir.exists():
            continue
        _import_jquants_dir(conn, raw_dir / "jquants", counters)
        _import_edinet_dir(conn, raw_dir / "edinet", counters)
        _import_jpx_dir(conn, raw_dir / "jpx", counters)
    conn.commit()
    return _summary_from_counters(counters)


def _record_table_integrity(conn: sqlite3.Connection) -> None:
    for table in _DATA_TABLES:
        row = conn.execute(_TABLE_COUNT_SQL[table]).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO cache_metadata(key, value) VALUES(?, ?)",
            (f"table_count.{table}", str(int(row[0] or 0))),
        )


def _table_integrity_is_stale(conn: sqlite3.Connection) -> bool:
    for table in _DATA_TABLES:
        recorded = conn.execute(
            "SELECT value FROM cache_metadata WHERE key = ?",
            (f"table_count.{table}",),
        ).fetchone()
        if recorded is None:
            return True
        try:
            expected_count = int(recorded[0])
        except (TypeError, ValueError):
            return True
        actual = conn.execute(_TABLE_COUNT_SQL[table]).fetchone()
        if int(actual[0] or 0) != expected_count:
            return True
    return False


def _summary_from_counters(counters: _RebuildCounters) -> RebuildSummary:
    return RebuildSummary(
        daily_bars_files=counters.bars_files,
        daily_bars_rows=counters.bars_rows,
        fin_summary_files=counters.fin_files,
        fin_summary_rows=counters.fin_rows,
        master_files=counters.master_files,
        master_rows=counters.master_rows,
        earnings_calendar_files=counters.earnings_files,
        earnings_calendar_rows=counters.earnings_rows,
        market_calendar_files=counters.market_calendar_files,
        market_calendar_rows=counters.market_calendar_rows,
        edinet_document_files=counters.edinet_doc_files,
        edinet_document_rows=counters.edinet_doc_rows,
        edinet_metric_files=counters.edinet_metric_files,
        edinet_metric_rows=counters.edinet_metric_rows,
        jpx_regulation_files=counters.jpx_reg_files,
        jpx_regulation_rows=counters.jpx_reg_rows,
        skipped_files=tuple(counters.skipped),
    )


def _iter_importable_raw_paths(raw_dir: Path) -> Iterator[Path]:
    jquants_dir = raw_dir / "jquants"
    if jquants_dir.exists():
        for path in sorted(jquants_dir.glob("*.json")):
            if _raw_file_source(path) is not None:
                yield path
    for child_dir in (raw_dir / "edinet" / "documents", raw_dir / "edinet" / "metrics"):
        if child_dir.exists():
            yield from sorted(child_dir.glob("*.json"))
    jpx_regulations_dir = raw_dir / "jpx" / "regulations"
    if jpx_regulations_dir.exists():
        yield from sorted(jpx_regulations_dir.glob("*.json"))


def _path_key(path: Path | str) -> str:
    return Path(path).resolve(strict=False).as_posix()


def _live_raw_files_by_path(raw_dirs: Iterable[Path]) -> dict[str, tuple[Path, str]]:
    return {
        _path_key(path): (path, hashlib.sha256(path.read_bytes()).hexdigest())
        for raw_root in raw_dirs
        if raw_root.exists()
        for path in _iter_importable_raw_paths(raw_root)
    }


def _new_raw_files_require_rebuild(conn: sqlite3.Connection, new_paths: Iterable[Path]) -> bool:
    existing_windows = _raw_import_windows_by_source(conn)
    existing_sources = set(existing_windows)
    new_single_snapshot_sources: set[str] = set()
    new_windows: dict[str, list[tuple[date, date]]] = {}
    for path in new_paths:
        source = _raw_file_source(path)
        if source is None:
            continue
        if source in _SINGLE_SNAPSHOT_SOURCES and source in existing_sources:
            return True
        if source in _SINGLE_SNAPSHOT_SOURCES:
            if source in new_single_snapshot_sources:
                return True
            new_single_snapshot_sources.add(source)
            continue
        window = _request_window_for_file(source, path)
        if window is None:
            if source in existing_sources:
                return True
            continue
        for existing_window in existing_windows.get(source, ()):
            if _windows_overlap(window, existing_window):
                return True
        for seen_window in new_windows.get(source, ()):
            if _windows_overlap(window, seen_window):
                return True
        new_windows.setdefault(source, []).append(window)
    return False


def _raw_import_windows_by_source(conn: sqlite3.Connection) -> dict[str, list[tuple[date, date]]]:
    try:
        rows = conn.execute("SELECT source, path, min_date, max_date FROM raw_imports").fetchall()
    except sqlite3.OperationalError:
        return {}
    windows_by_source: dict[str, list[tuple[date, date]]] = {}
    for source, path_text, min_date, max_date in rows:
        source_text = str(source)
        window = _request_window_for_file(source_text, Path(str(path_text)))
        if window is None and min_date and max_date:
            window = _date_window(str(min_date), str(max_date))
        if window is not None:
            windows_by_source.setdefault(source_text, []).append(window)
        else:
            windows_by_source.setdefault(source_text, [])
    return windows_by_source


_CHUNK_WINDOW_RE = re.compile(r"end_dt-(\d{4}-\d{2}-\d{2}).*?start_dt-(\d{4}-\d{2}-\d{2})")
_MKT_CALENDAR_WINDOW_RE = re.compile(r"from_yyyymmdd-(\d{8}).*?to_yyyymmdd-(\d{8})")


def _request_window_for_file(source: str, path: Path) -> tuple[date, date] | None:
    name = path.name
    if source in {"jquants_daily_bars", "jquants_fin_summaries"}:
        match = _CHUNK_WINDOW_RE.search(name)
        if match is None:
            return None
        return _date_window(match.group(2), match.group(1))
    if source == "jquants_market_calendar":
        match = _MKT_CALENDAR_WINDOW_RE.search(name)
        if match is None:
            return None
        return _date_window(
            f"{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:]}",
            f"{match.group(2)[:4]}-{match.group(2)[4:6]}-{match.group(2)[6:]}",
        )
    if source in {"edinet_documents", "edinet_metrics", "jpx_regulation_flags"}:
        return _date_window(path.stem, path.stem)
    return None


def _date_window(start_text: str, end_text: str) -> tuple[date, date] | None:
    try:
        return date.fromisoformat(start_text), date.fromisoformat(end_text)
    except ValueError:
        return None


def _windows_overlap(left: tuple[date, date], right: tuple[date, date]) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def _raw_file_source(path: Path) -> str | None:
    parent = path.parent
    name = path.name
    if parent.name == "jquants":
        if name.startswith("get_eq_bars_daily_range"):
            return "jquants_daily_bars"
        if name.startswith("get_fin_summary_range"):
            return "jquants_fin_summaries"
        if name == "get_eq_master.json":
            return "jquants_master_snapshots"
        if name.startswith("get_eq_earnings_cal"):
            return "jquants_earnings_calendar"
        if name.startswith("get_mkt_calendar"):
            return "jquants_market_calendar"
    if parent.name == "documents" and parent.parent.name == "edinet":
        return "edinet_documents"
    if parent.name == "metrics" and parent.parent.name == "edinet":
        return "edinet_metrics"
    if parent.name == "regulations" and parent.parent.name == "jpx":
        return "jpx_regulation_flags"
    return None


def _raw_import_sha_by_path(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute("SELECT path, sha256 FROM raw_imports").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {_path_key(str(path)): str(sha) for path, sha in rows}


def _path_is_under(path_text: str, raw_root: Path) -> bool:
    raw_prefix = _path_key(raw_root).rstrip("/") + "/"
    return _path_key(path_text).startswith(raw_prefix)


def _path_is_under_any(path_text: str, raw_roots: Iterable[Path]) -> bool:
    return any(_path_is_under(path_text, raw_root) for raw_root in raw_roots)


def _import_raw_file(conn: sqlite3.Connection, path: Path, counters: _RebuildCounters) -> None:
    source = _raw_file_source(path)
    name = path.name
    try:
        if source == "jquants_daily_bars":
            counters.bars_rows += _import_bars_file(conn, path)
            counters.bars_files += 1
        elif source == "jquants_fin_summaries":
            counters.fin_rows += _import_fin_summary_file(conn, path)
            counters.fin_files += 1
        elif source == "jquants_master_snapshots":
            counters.master_rows += _import_master_file(conn, path)
            counters.master_files += 1
        elif source == "jquants_earnings_calendar":
            counters.earnings_rows += _import_earnings_calendar_file(conn, path)
            counters.earnings_files += 1
        elif source == "jquants_market_calendar":
            counters.market_calendar_rows += _import_market_calendar_file(conn, path)
            counters.market_calendar_files += 1
        elif source == "edinet_documents":
            counters.edinet_doc_rows += _import_edinet_documents_file(conn, path)
            counters.edinet_doc_files += 1
        elif source == "edinet_metrics":
            counters.edinet_metric_rows += _import_edinet_metrics_file(conn, path)
            counters.edinet_metric_files += 1
        elif source == "jpx_regulation_flags":
            counters.jpx_reg_rows += _import_jpx_regulations_file(conn, path)
            counters.jpx_reg_files += 1
        else:
            counters.skipped.append(name)
    except (JQuantsProviderError, ValueError, KeyError, TypeError) as exc:
        raise SQLiteCacheError(f"failed to import {path.name}: {exc}") from exc


def _import_jquants_dir(
    conn: sqlite3.Connection, jquants_dir: Path, counters: _RebuildCounters
) -> None:
    if not jquants_dir.exists():
        return
    for path in sorted(jquants_dir.glob("*.json")):
        name = path.name
        try:
            if name.startswith("get_eq_bars_daily_range"):
                counters.bars_rows += _import_bars_file(conn, path)
                counters.bars_files += 1
            elif name.startswith("get_fin_summary_range"):
                counters.fin_rows += _import_fin_summary_file(conn, path)
                counters.fin_files += 1
            elif name == "get_eq_master.json":
                counters.master_rows += _import_master_file(conn, path)
                counters.master_files += 1
            elif name.startswith("get_eq_earnings_cal"):
                counters.earnings_rows += _import_earnings_calendar_file(conn, path)
                counters.earnings_files += 1
            elif name.startswith("get_mkt_calendar"):
                counters.market_calendar_rows += _import_market_calendar_file(conn, path)
                counters.market_calendar_files += 1
            else:
                counters.skipped.append(name)
        except (JQuantsProviderError, ValueError, KeyError, TypeError) as exc:
            raise SQLiteCacheError(f"failed to import {path.name}: {exc}") from exc


def _import_edinet_dir(
    conn: sqlite3.Connection, edinet_dir: Path, counters: _RebuildCounters
) -> None:
    if not edinet_dir.exists():
        return
    documents_dir = edinet_dir / "documents"
    if documents_dir.exists():
        for path in sorted(documents_dir.glob("*.json")):
            try:
                counters.edinet_doc_rows += _import_edinet_documents_file(conn, path)
                counters.edinet_doc_files += 1
            except (ValueError, KeyError, TypeError) as exc:
                raise SQLiteCacheError(f"failed to import {path.name}: {exc}") from exc
    metrics_dir = edinet_dir / "metrics"
    if metrics_dir.exists():
        for path in sorted(metrics_dir.glob("*.json")):
            try:
                counters.edinet_metric_rows += _import_edinet_metrics_file(conn, path)
                counters.edinet_metric_files += 1
            except (ValueError, KeyError, TypeError) as exc:
                raise SQLiteCacheError(f"failed to import {path.name}: {exc}") from exc


def _import_jpx_dir(conn: sqlite3.Connection, jpx_dir: Path, counters: _RebuildCounters) -> None:
    if not jpx_dir.exists():
        return
    regulations_dir = jpx_dir / "regulations"
    if not regulations_dir.exists():
        return
    for path in sorted(regulations_dir.glob("*.json")):
        try:
            counters.jpx_reg_rows += _import_jpx_regulations_file(conn, path)
            counters.jpx_reg_files += 1
        except (ValueError, KeyError, TypeError) as exc:
            raise SQLiteCacheError(f"failed to import {path.name}: {exc}") from exc


def _earnings_calendar_rows(records: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for record in records:
        ticker = _normalize_ticker_or_none(_first(record, "Code", "code"))
        announcement_date = _date_iso(
            _first(record, "Date", "date", "AnnouncementDate", "announcement_date")
        )
        if ticker is None or announcement_date is None:
            continue
        rows.append(
            (
                announcement_date,
                ticker,
                json.dumps(record, ensure_ascii=False, sort_keys=True),
            )
        )
    return rows


def _market_calendar_rows(records: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for record in records:
        day = _date_iso(_first(record, "Date", "date"))
        if day is None:
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
                json.dumps(record, ensure_ascii=False, sort_keys=True),
            )
        )
    return rows


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
                json.dumps(record, ensure_ascii=False, sort_keys=True),
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


def _import_bars_file(conn: sqlite3.Connection, path: Path) -> int:
    records = _read_json_array(path)
    rows = list(_iter_bars_rows(records))
    if not rows:
        _record_raw_import(conn, "jquants_daily_bars", path, 0, None, None)
        return 0
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
    traded_dates = sorted({row[1] for row in rows})
    _record_raw_import(
        conn,
        "jquants_daily_bars",
        path,
        len(rows),
        traded_dates[0],
        traded_dates[-1],
    )
    return len(rows)


def _iter_bars_rows(
    records: Iterable[Mapping[str, Any]],
) -> Iterator[tuple[Any, ...]]:
    for record in records:
        ticker = _normalize_ticker_or_none(_first(record, "Code", "code"))
        traded_at = _date_iso(_first(record, "Date", "date", "TradedAt", "traded_at"))
        if ticker is None or traded_at is None:
            continue
        yield (
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
            _to_float(_first(record, "AdjustmentVolume", "adjustment_volume", "AdjVo", "adj_vo")),
            _to_float(_first(record, "AdjustmentFactor", "adjustment_factor", "AdjFactor")),
            _to_str_or_none(_first(record, "UpperLimit", "upper_limit", "UL")),
            _to_str_or_none(_first(record, "LowerLimit", "lower_limit", "LL")),
        )


def _import_fin_summary_file(conn: sqlite3.Connection, path: Path) -> int:
    records = _read_json_array(path)
    rows = list(_iter_fin_summary_rows(records))
    if not rows:
        _record_raw_import(conn, "jquants_fin_summaries", path, 0, None, None)
        return 0
    conn.executemany(
        """
        INSERT OR REPLACE INTO jquants_fin_summaries(
          ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding,
          sales, cfo, cash_eq, total_assets, equity, operating_profit, ordinary_profit, profit,
          fiscal_period, fiscal_year_end, period_start, period_end, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    disclosed_dates = sorted({row[1] for row in rows})
    _record_raw_import(
        conn,
        "jquants_fin_summaries",
        path,
        len(rows),
        disclosed_dates[0],
        disclosed_dates[-1],
    )
    return len(rows)


def _iter_fin_summary_rows(
    records: Iterable[Mapping[str, Any]],
) -> Iterator[tuple[Any, ...]]:
    for record in records:
        ticker = _normalize_ticker_or_none(_first(record, "Code", "code"))
        disclosed_at = _date_iso(
            _first(record, "DisclosedDate", "disclosed_at", "DiscDate", "disc_date", "Date")
        )
        if ticker is None or disclosed_at is None:
            continue
        yield (
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
                    record, "CurrentFiscalYearEndDate", "current_fiscal_year_end_date", "CurFYEn"
                )
            ),
            _date_iso(
                _first(record, "CurrentPeriodStartDate", "current_period_start_date", "CurPerSt")
            ),
            _date_iso(
                _first(record, "CurrentPeriodEndDate", "current_period_end_date", "CurPerEn")
            ),
            json.dumps(record, ensure_ascii=False, sort_keys=True),
        )


def _import_master_file(conn: sqlite3.Connection, path: Path) -> int:
    records = _read_json_array(path)
    rows = list(_iter_master_rows(records))
    if not rows:
        _record_raw_import(conn, "jquants_master_snapshots", path, 0, None, None)
        return 0
    conn.executemany(
        """
        INSERT OR REPLACE INTO jquants_master_snapshots(
          snapshot_date, ticker, name, market, sector_33, is_common_stock, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    snapshot_dates = sorted({row[0] for row in rows})
    _record_raw_import(
        conn,
        "jquants_master_snapshots",
        path,
        len(rows),
        snapshot_dates[0],
        snapshot_dates[-1],
    )
    return len(rows)


def _iter_master_rows(
    records: Iterable[Mapping[str, Any]],
) -> Iterator[tuple[Any, ...]]:
    for record in records:
        ticker = _normalize_ticker_or_none(_first(record, "Code", "code"))
        if ticker is None:
            continue
        snapshot_date = _date_iso(_first(record, "Date", "date", "snapshot_date")) or "unknown"
        is_common_stock = _is_common_stock_flag(record)
        sector_raw = _to_str_or_none(
            _first(record, "Sector33CodeName", "sector_33", "Sector33Name", "S33Nm", "S33")
        )
        yield (
            snapshot_date,
            ticker,
            _to_str_or_none(_first(record, "CompanyName", "company_name", "Name", "CoName")),
            _to_str_or_none(_first(record, "MarketCodeName", "market_segment", "MktNm", "Mkt")),
            # J-Quants は同じ TSE 33 セクターを半角中黒 (U+FF65)・全角中黒 (U+30FB) で
            # 揺らせて返してくる。SQLite に取り込む段階で全角形に正規化し、outlook /
            # candidates / select の matcher が一意に解決できるようにする。
            normalize_sector_name(sector_raw) if sector_raw else sector_raw,
            1 if is_common_stock else 0,
            json.dumps(record, ensure_ascii=False, sort_keys=True),
        )


def _import_earnings_calendar_file(conn: sqlite3.Connection, path: Path) -> int:
    records = _read_json_array(path)
    rows = _earnings_calendar_rows(records)
    if not rows:
        _record_raw_import(conn, "jquants_earnings_calendar", path, 0, None, None)
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO jquants_earnings_calendar("
        "announcement_date, ticker, raw_json"
        ") VALUES (?, ?, ?)",
        rows,
    )
    dates = sorted({row[0] for row in rows})
    _record_raw_import(
        conn,
        "jquants_earnings_calendar",
        path,
        len(rows),
        dates[0],
        dates[-1],
    )
    return len(rows)


def _import_market_calendar_file(conn: sqlite3.Connection, path: Path) -> int:
    records = _read_json_array(path)
    rows = _market_calendar_rows(records)
    if not rows:
        _record_raw_import(conn, "jquants_market_calendar", path, 0, None, None)
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO jquants_market_calendar(day, is_business_day, raw_json) "
        "VALUES (?, ?, ?)",
        rows,
    )
    days = sorted({row[0] for row in rows})
    _record_raw_import(conn, "jquants_market_calendar", path, len(rows), days[0], days[-1])
    return len(rows)


def _import_edinet_documents_file(conn: sqlite3.Connection, path: Path) -> int:
    records = _read_json_array(path)
    # The filename is `{doc_date}.json`; the API payload itself does not
    # always carry the date so we recover it from the filename stem.
    doc_date = path.stem
    rows = _edinet_document_rows(doc_date, records)
    if not rows:
        _record_raw_import(conn, "edinet_documents", path, 0, doc_date, doc_date)
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO edinet_documents("
        "doc_date, doc_id, sec_code, doc_type_code, raw_json"
        ") VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    _record_raw_import(conn, "edinet_documents", path, len(rows), doc_date, doc_date)
    return len(rows)


def _import_edinet_metrics_file(conn: sqlite3.Connection, path: Path) -> int:
    records = _read_json_array(path)
    asof_date = path.stem
    rows = _edinet_metric_rows(asof_date, records)
    if not rows:
        _record_raw_import(conn, "edinet_metrics", path, 0, asof_date, asof_date)
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO edinet_metrics("
        "asof_date, ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
        "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, ttm_quality_pcfr, "
        "operating_profit_ttm, depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, "
        "net_cash, equity, total_assets, ttm_quality_fcf, ttm_quality_net_cash, "
        "source_doc_id, document_type, source_submit_datetime, source_period_start, "
        "source_period_end, capex_source, failure_reasons"
        ") VALUES ("
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
        ")",
        rows,
    )
    _record_raw_import(conn, "edinet_metrics", path, len(rows), asof_date, asof_date)
    return len(rows)


def _import_jpx_regulations_file(conn: sqlite3.Connection, path: Path) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise SQLiteCacheError(f"expected JSON object at {path}")
    asof_date = path.stem
    fetched_at_utc = _to_str_or_none(payload.get("fetched_at_utc"))
    flags_by_ticker = payload.get("flags_by_ticker") or {}
    if not isinstance(flags_by_ticker, Mapping):
        raise SQLiteCacheError(f"flags_by_ticker must be an object: {path}")
    source_names = payload.get("source_names") or []
    if not isinstance(source_names, list | tuple):
        raise SQLiteCacheError(f"source_names must be an array: {path}")
    source_rows = [
        (asof_date, source_name, fetched_at_utc)
        for raw_source_name in source_names
        if (source_name := _to_str_or_none(raw_source_name)) is not None
    ]
    if source_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO jpx_regulation_sources("
            "asof_date, source_name, fetched_at_utc"
            ") VALUES (?, ?, ?)",
            source_rows,
        )
    rows: list[tuple[Any, ...]] = []
    for raw_ticker, flags in flags_by_ticker.items():
        ticker = _normalize_ticker_or_none(raw_ticker)
        if ticker is None:
            continue
        if not isinstance(flags, list | tuple):
            continue
        for flag in flags:
            flag_text = _to_str_or_none(flag)
            if flag_text is None:
                continue
            # JPX cache JSON groups flags per ticker without recording the
            # source URL that produced each flag. Use the flag string as the
            # source_name so the (asof, source, ticker, flag) PK stays unique
            # while preserving the natural-language label for downstream UI.
            rows.append((asof_date, flag_text, ticker, flag_text, fetched_at_utc))
    if not rows:
        _record_raw_import(conn, "jpx_regulation_flags", path, 0, asof_date, asof_date)
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO jpx_regulation_flags("
        "asof_date, source_name, ticker, flag, fetched_at_utc"
        ") VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    _record_raw_import(conn, "jpx_regulation_flags", path, len(rows), asof_date, asof_date)
    return len(rows)


def _record_raw_import(
    conn: sqlite3.Connection,
    source: str,
    path: Path,
    record_count: int,
    min_date: str | None,
    max_date: str | None,
) -> None:
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    conn.execute(
        """
        INSERT OR REPLACE INTO raw_imports(
          source, path, sha256, imported_at_utc, record_count, min_date, max_date
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            _path_key(path),
            sha,
            datetime.now(UTC).isoformat(),
            record_count,
            min_date,
            max_date,
        ),
    )
    _record_source_coverage_for_legacy_raw_import(
        conn,
        source=source,
        path=path,
        record_count=record_count,
        min_date=min_date,
        max_date=max_date,
    )


def _record_source_coverage_for_legacy_raw_import(
    conn: sqlite3.Connection,
    *,
    source: str,
    path: Path,
    record_count: int,
    min_date: str | None,
    max_date: str | None,
) -> None:
    window = _request_window_for_file(source, path)
    coverage_start = window[0].isoformat() if window else min_date
    coverage_end = window[1].isoformat() if window else max_date
    operation = _legacy_operation_for_source(source)
    if operation is None:
        return
    _record_source_coverage(
        conn,
        source=source,
        operation=operation,
        coverage_key=(
            _range_coverage_key(operation, window[0], window[1])
            if window
            else path.stem
            if source in {"edinet_documents", "edinet_metrics", "jpx_regulation_flags"}
            else path.name
        ),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        requested_start=coverage_start,
        requested_end=coverage_end,
        params={"legacy_raw_path": _path_key(path)},
        record_count=record_count,
        raw_record_count=record_count,
        skipped_record_count=0,
        replace=False,
    )


def _record_source_coverage(
    conn: sqlite3.Connection,
    *,
    source: str,
    operation: str,
    coverage_key: str,
    coverage_start: str | None,
    coverage_end: str | None,
    requested_start: str | None,
    requested_end: str | None,
    params: Mapping[str, Any],
    record_count: int,
    raw_record_count: int | None = None,
    skipped_record_count: int = 0,
    status: str = "ok",
    error: str | None = None,
    fetched_at_utc: str | None = None,
    replace: bool = True,
) -> None:
    normalized_record_count = record_count
    conflict_action = "REPLACE" if replace else "IGNORE"
    conn.execute(
        f"""
        INSERT OR {conflict_action} INTO source_coverage(
          source, operation, coverage_key, coverage_start, coverage_end,
          requested_start, requested_end, params_json, fetched_at_utc, record_count,
          raw_record_count, normalized_record_count, skipped_record_count, status, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            operation,
            coverage_key,
            coverage_start,
            coverage_end,
            requested_start,
            requested_end,
            json.dumps(params, ensure_ascii=False, sort_keys=True),
            fetched_at_utc or datetime.now(UTC).isoformat(),
            record_count,
            raw_record_count if raw_record_count is not None else record_count,
            normalized_record_count,
            skipped_record_count,
            status,
            error,
        ),
    )


def _migrate_source_coverage_from_raw_imports(conn: sqlite3.Connection) -> None:
    try:
        migrated = conn.execute(
            "SELECT value FROM cache_metadata WHERE key = ?",
            (_RAW_IMPORT_COVERAGE_MIGRATION_KEY,),
        ).fetchone()
        if migrated is not None and migrated[0] == SCHEMA_VERSION:
            return
        rows = conn.execute(
            "SELECT source, path, record_count, min_date, max_date FROM raw_imports"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    if not rows:
        return
    for source, path_text, record_count, min_date, max_date in rows:
        _record_source_coverage_for_legacy_raw_import(
            conn,
            source=str(source),
            path=Path(str(path_text)),
            record_count=int(record_count or 0),
            min_date=str(min_date) if min_date else None,
            max_date=str(max_date) if max_date else None,
        )
    conn.execute(
        "INSERT OR REPLACE INTO cache_metadata(key, value) VALUES(?, ?)",
        (_RAW_IMPORT_COVERAGE_MIGRATION_KEY, SCHEMA_VERSION),
    )


def _legacy_operation_for_source(source: str) -> str | None:
    return {
        "jquants_daily_bars": "get_eq_bars_daily_range",
        "jquants_fin_summaries": "get_fin_summary_range",
        "jquants_master_snapshots": "get_eq_master",
        "jquants_earnings_calendar": "get_eq_earnings_cal",
        "jquants_market_calendar": "get_mkt_calendar",
        "edinet_documents": "documents",
        "edinet_metrics": "metrics",
        "jpx_regulation_flags": "regulations",
    }.get(source)


def _range_coverage_key(operation: str, start: date, end: date) -> str:
    return f"{operation}:{start.isoformat()}..{end.isoformat()}"


def _read_json_array(path: Path) -> list[Mapping[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SQLiteCacheError(f"expected JSON array at {path}, got {type(payload).__name__}")
    return [item for item in payload if isinstance(item, Mapping)]


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
