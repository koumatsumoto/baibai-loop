"""SQLite cache layer rebuilt from `data/raw/screening/` raw JSON.

Issue #45 keeps raw JSON as the canonical, audit-grade source under git, and
treats SQLite as a derived workspace cache that can be rebuilt at any time.
This module owns:

- the SQLite schema (versioned via `SCHEMA_VERSION`)
- per-source readers that translate raw JSON into rows
- the top-level `rebuild_from_raw()` that scans a `data/raw/screening/` tree
  and writes a fresh SQLite file from scratch

EDINET / JPX / earnings_calendar / market_calendar tables are out of scope
for this iteration; see issue #45 follow-ups.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .providers.jquants import (
    JQuantsProviderError,
    parse_jquants_code,
)

SCHEMA_VERSION = "v1"

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


def rebuild_from_raw(raw_dir: Path, db_path: Path) -> RebuildSummary:
    """Rebuild the SQLite cache from scratch using every `*.json` under
    `raw_dir/jquants/`.

    The destination file is removed first so the rebuild is deterministic and
    will not carry stale rows from a prior schema version.
    """
    if db_path.exists():
        db_path.unlink()
    conn = open_connection(db_path)
    try:
        return _rebuild(conn, raw_dir)
    finally:
        conn.close()


def _rebuild(conn: sqlite3.Connection, raw_dir: Path) -> RebuildSummary:
    jquants_dir = raw_dir / "jquants"
    bars_files = 0
    bars_rows = 0
    fin_files = 0
    fin_rows = 0
    master_files = 0
    master_rows = 0
    skipped: list[str] = []

    if not jquants_dir.exists():
        return RebuildSummary(
            daily_bars_files=0,
            daily_bars_rows=0,
            fin_summary_files=0,
            fin_summary_rows=0,
            master_files=0,
            master_rows=0,
            skipped_files=(),
        )

    for path in sorted(jquants_dir.glob("*.json")):
        name = path.name
        try:
            if name.startswith("get_eq_bars_daily_range"):
                bars_rows += _import_bars_file(conn, path)
                bars_files += 1
            elif name.startswith("get_fin_summary_range"):
                fin_rows += _import_fin_summary_file(conn, path)
                fin_files += 1
            elif name == "get_eq_master.json":
                master_rows += _import_master_file(conn, path)
                master_files += 1
            else:
                skipped.append(name)
        except (JQuantsProviderError, ValueError, KeyError, TypeError) as exc:
            raise SQLiteCacheError(f"failed to import {path.name}: {exc}") from exc

    conn.commit()
    return RebuildSummary(
        daily_bars_files=bars_files,
        daily_bars_rows=bars_rows,
        fin_summary_files=fin_files,
        fin_summary_rows=fin_rows,
        master_files=master_files,
        master_rows=master_rows,
        skipped_files=tuple(skipped),
    )


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
        yield (
            snapshot_date,
            ticker,
            _to_str_or_none(_first(record, "CompanyName", "company_name", "Name", "CoName")),
            _to_str_or_none(_first(record, "MarketCodeName", "market_segment", "MktNm", "Mkt")),
            _to_str_or_none(
                _first(record, "Sector33CodeName", "sector_33", "Sector33Name", "S33Nm", "S33")
            ),
            1 if is_common_stock else 0,
            json.dumps(record, ensure_ascii=False, sort_keys=True),
        )


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
