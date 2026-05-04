"""SQLite cache layer rebuilt from `records/_data/raw/screening/` raw JSON.

Issue #45 keeps raw JSON as the canonical, audit-grade source under git, and
treats SQLite as a derived workspace cache that can be rebuilt at any time.
This module owns:

- the SQLite schema (versioned via `SCHEMA_VERSION`)
- per-source readers that translate raw JSON into rows
- the top-level `rebuild_from_raw()` that scans a `records/_data/raw/screening/` tree
  and writes a fresh SQLite file from scratch

Schema v2 (current) covers all five sources: jquants daily bars / fin
summaries / master / earnings calendar / market calendar, plus EDINET
documents and metrics, and JPX regulation flags.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .providers.jquants import (
    JQuantsProviderError,
    normalize_sector_name,
    parse_jquants_code,
)

SCHEMA_VERSION = "v2"

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

CREATE TABLE IF NOT EXISTS raw_imports(
  source TEXT NOT NULL,
  path TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  imported_at_utc TEXT NOT NULL,
  record_count INTEGER NOT NULL,
  min_date TEXT,
  max_date TEXT
);

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
    conn.execute(
        "INSERT OR REPLACE INTO cache_metadata(key, value) VALUES('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    return conn


def rebuild_from_raw(raw_dir: Path | Iterable[Path], db_path: Path) -> RebuildSummary:
    """Rebuild the SQLite cache from scratch by walking each entry in
    `raw_dir` (a single Path or an iterable of Paths). Files are dispatched
    by their parent directory + filename pattern (see `_rebuild`).

    Multiple raw dirs are supported so the legacy `.cache/screening/` tree
    can be merged with the canonical `records/_data/raw/screening/` tree
    in a single SQLite, without forcing the operator to migrate files.
    Chunk windows differ between the two (legacy was last populated with a
    different asof), so merging gives the SQLite reader broader coverage.

    The destination file is removed first so the rebuild is deterministic
    and will not carry stale rows from a prior schema version.
    """
    if db_path.exists():
        db_path.unlink()
    raw_dirs: tuple[Path, ...] = (raw_dir,) if isinstance(raw_dir, Path) else tuple(raw_dir)
    conn = open_connection(db_path)
    try:
        return _rebuild(conn, raw_dirs)
    finally:
        conn.close()


def is_sqlite_stale(raw_dirs: Iterable[Path], db_path: Path) -> bool:
    """True when the SQLite cache is missing or older than the newest raw
    JSON file across all `raw_dirs`. Used by the `run` command to
    auto-rebuild before any reader consults SQLite — without this, JSON
    chunk filenames keyed by asof-relative windows force a full 1200-day
    refetch when asof shifts by even one day, since chunk windows then no
    longer match cached files. SQLite is range-aware and absorbs that
    drift.
    """
    if not db_path.exists():
        return True
    db_mtime = db_path.stat().st_mtime
    for raw_dir in raw_dirs:
        if not raw_dir.exists():
            continue
        for path in raw_dir.rglob("*.json"):
            if path.stat().st_mtime > db_mtime:
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
          sales, operating_profit, ordinary_profit, profit,
          fiscal_period, fiscal_year_end, period_start, period_end, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            )
        )
    if not rows:
        _record_raw_import(conn, "edinet_metrics", path, 0, asof_date, asof_date)
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO edinet_metrics("
        "asof_date, ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
        "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, ttm_quality_pcfr"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            path.as_posix(),
            sha,
            datetime.now(UTC).isoformat(),
            record_count,
            min_date,
            max_date,
        ),
    )


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
    except TypeError, ValueError:
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
