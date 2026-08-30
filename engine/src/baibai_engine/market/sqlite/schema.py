"""Current-schema DDL and shape validation for the rebuildable market cache.

This is the single physical store (`market.sqlite`): the market price/calendar
tables and the screening fundamentals/regulation tables share one file, one
schema version, and one connection path. Market owns the schema so the
price-data layer can open and validate the store without importing screening,
while screening reuses the same DDL for its fundamentals tables.

The cache is rebuilt or replaced explicitly when this contract changes. Runtime
readers never interpret or migrate an older shape.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

SQLITE_SCHEMA_VERSION = 25
SCHEMA_VERSION = str(SQLITE_SCHEMA_VERSION)

# EDINET serves a filing's descriptive columns only while its public-inspection period
# runs. Once that period ends the day's list still returns the entry with these five
# nulled — measured over the whole store, a row that has run out carries none of them,
# while `doc_type_code`, `submit_datetime` and `doc_description` are present on every row
# that has not. The converse does not hold for the other two: a filing can genuinely have
# no securities code and no parent, so a null there is not by itself evidence of expiry.
# The company, form, timestamp and title a filing carried do not stop being true when
# they stop being served, so a store that once observed them keeps them — the ingest
# refuses to blank a stored value on a later list, and the merge fills a null from
# whichever copy read the day while it was still served. Two populated values that
# disagree still refuse the merge, which is what makes a genuine corruption visible.
EDINET_DOCUMENT_DESCRIPTIVE_COLUMNS: tuple[str, ...] = (
    "sec_code",
    "doc_type_code",
    "parent_doc_id",
    "submit_datetime",
    "doc_description",
)

# EDINET also stops serving these filing identities once the inspection period ends.
# They remain true of the filing just like its descriptive columns, so ingest retains
# an identity it has observed instead of replacing it with a later null response.
EDINET_DOCUMENT_IDENTITY_COLUMNS: tuple[str, ...] = (
    "edinet_code",
    "issuer_edinet_code",
    "subject_edinet_code",
)

EDINET_DOCUMENT_RETAINED_COLUMNS: tuple[str, ...] = (
    *EDINET_DOCUMENT_DESCRIPTIVE_COLUMNS,
    *EDINET_DOCUMENT_IDENTITY_COLUMNS,
)

# What EDINET currently does with the filing rather than what the filing says: whether
# the inspection period is running, whether the document files are still downloadable,
# and whether the filing has been withdrawn. Two stores that read the same day at
# different moments differ here by construction and the later read is the accurate one,
# so the ingest always takes the new answer and the merge does not compare them.
EDINET_DOCUMENT_LIFECYCLE_COLUMNS: tuple[str, ...] = (
    "legal_status",
    "withdrawal_status",
    "csv_flag",
    "xbrl_flag",
)

_REQUIRED_TABLES = (
    "jquants_daily_bars",
    "jquants_fin_summaries",
    "jquants_master_snapshots",
    "jpx_earnings_calendar",
    "jquants_market_calendar",
    "jquants_weekly_margin",
    "jquants_margin_alerts",
    "jquants_all_issues_daily_margin",
    "jquants_short_sale_reports",
    "edinet_documents",
    "edinet_document_lists",
    "edinet_metrics",
    "tse_capital_policy_snapshots",
    "jpx_delistings",
    "tender_offer_exit_values",
    "jpx_regulation_flags",
    "jpx_regulation_sources",
    "source_coverage",
    "lake_store_origin",
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
        "forecast_profit",
        "forecast_ordinary_profit",
        "fiscal_period",
        "fiscal_year_end",
        "period_start",
        "period_end",
        "dps_actual_annual",
        "dps_forecast_annual",
        "treasury_shares",
        "equity_to_asset_ratio",
        "dividend_q1",
        "dividend_interim",
        "dividend_q3",
        "dividend_year_end",
        "dividend_total_annual",
        "average_shares",
    ),
    "jquants_master_snapshots": (
        "snapshot_date",
        "ticker",
        "name",
        "market",
        "sector_33",
        "is_common_stock",
    ),
    "jpx_earnings_calendar": ("announcement_date", "ticker"),
    "jquants_market_calendar": ("day", "is_business_day"),
    "jquants_weekly_margin": (
        "week_end",
        "ticker",
        "long_vol",
        "short_vol",
        "long_std_vol",
        "long_neg_vol",
        "short_std_vol",
        "short_neg_vol",
        "issue_type",
    ),
    "jquants_margin_alerts": (
        "publication_date",
        "ticker",
        "applied_date",
        "publication_reason",
        "short_outstanding",
        "short_change",
        "short_ratio",
        "long_outstanding",
        "long_change",
        "long_ratio",
        "short_long_ratio",
        "short_negotiable_outstanding",
        "short_negotiable_change",
        "short_standard_outstanding",
        "short_standard_change",
        "long_negotiable_outstanding",
        "long_negotiable_change",
        "long_standard_outstanding",
        "long_standard_change",
        "tse_margin_regulation_classification",
    ),
    "jquants_all_issues_daily_margin": (
        "balance_date",
        "ticker",
        "long_vol",
        "short_vol",
        "long_std_vol",
        "long_neg_vol",
        "short_std_vol",
        "short_neg_vol",
        "issue_type",
    ),
    "jquants_short_sale_reports": (
        "disclosed_at",
        "source_ordinal",
        "calculated_at",
        "ticker",
        "short_seller_name",
        "discretionary_investment_contractor_name",
        "investment_fund_name",
        "short_ratio",
        "short_shares",
        "short_trading_units",
        "previous_reported_at",
        "previous_short_ratio",
        "is_cancellation",
        "notes",
    ),
    "edinet_documents": (
        "doc_date",
        "sequence_number",
        "doc_id",
        "sec_code",
        "doc_type_code",
        "csv_flag",
        "xbrl_flag",
        "legal_status",
        "disclosure_status",
        "withdrawal_status",
        "doc_info_edit_status",
        "parent_doc_id",
        "operation_datetime",
        "submit_datetime",
        "doc_description",
        "period_start",
        "period_end",
        "edinet_code",
        "issuer_edinet_code",
        "subject_edinet_code",
    ),
    "edinet_document_lists": (
        "doc_date",
        "process_datetime",
        "result_count",
        "fetched_at_utc",
        "is_final",
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
        "extractor_revision",
        "source_document_revision",
        "investment_securities",
    ),
    "jpx_regulation_flags": (
        "asof_date",
        "source_name",
        "ticker",
        "flag",
        "fetched_at_utc",
    ),
    "jpx_regulation_sources": ("asof_date", "source_name", "fetched_at_utc"),
    "tse_capital_policy_snapshots": (
        "snapshot_month_end",
        "ticker",
        "status",
        "status_change",
        "updated_on",
        "contact_requested",
        "first_disclosed_month_end",
        "first_disclosure_left_censored",
    ),
    "jpx_delistings": ("delisted_on", "ticker", "name", "market", "reason"),
    "tender_offer_exit_values": (
        "ticker",
        "delisted_on",
        "offer_price_yen",
        "offer_doc_id",
        "result_doc_id",
        "filed_on",
    ),
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
    "lake_store_origin": (
        "singleton",
        "release_id",
        "release_manifest_sha256",
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
  forecast_profit REAL,
  forecast_ordinary_profit REAL,
  fiscal_period TEXT,
  fiscal_year_end TEXT,
  period_start TEXT,
  period_end TEXT,
  dps_actual_annual REAL,
  dps_forecast_annual REAL,
  -- 期末自己株式数と、開示された自己資本比率。`shares_outstanding` は自己株式を含む
  -- 発行済株式総数、`equity` は非支配株主持分を含む純資産なので、時価総額と自己資本
  -- 比率をそれぞれ正しい分母で作るには両方が要る。
  treasury_shares REAL,
  equity_to_asset_ratio REAL,
  -- 支払ごとの 1 株当たり配当と、通期に支払った配当の総額 (円)。年間 DPS は中間・期末
  -- それぞれの基準日時点の株式基準で記載されるので、分割・併合を跨いだ年度は株価と同じ
  -- 基準か言えない。支払ごとに持てば、各支払の基準日 (四半期末) より後の調整だけを掛けて
  -- as-of 基準へ寄せられる。総額は円なので株式基準を持たず、自己株式を除いた株式数で
  -- 割った値がその換算の独立した照合になる。
  dividend_q1 REAL,
  dividend_interim REAL,
  dividend_q3 REAL,
  dividend_year_end REAL,
  dividend_total_annual REAL,
  -- 期中平均株式数。提出者が EPS を出すのに使った株数そのもので、期末発行済と自己株から
  -- 引いた株数が壊れていないかを同じ行の中で照合できる。`shares_outstanding` は期末の
  -- 発行済総数で、両者は別の量なので独立した列で持つ。
  average_shares REAL,
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

CREATE TABLE IF NOT EXISTS jpx_earnings_calendar(
  announcement_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  PRIMARY KEY (announcement_date, ticker)
);

CREATE TABLE IF NOT EXISTS jquants_market_calendar(
  day TEXT PRIMARY KEY,
  is_business_day INTEGER NOT NULL
);

-- Margin balances the exchange publishes once a week, as of that week's balance
-- date. `issue_type` is kept because it decides whether a short balance is even
-- possible: a 信用銘柄 (1) has no stock lending, so its zero short balance is a
-- property of the instrument rather than an observation about positioning, and
-- an axis built on the long/short ratio has to say which it is looking at.
-- Standard and negotiable margin are stored separately because only the standard
-- side carries a six-month settlement deadline.
CREATE TABLE IF NOT EXISTS jquants_weekly_margin(
  week_end TEXT NOT NULL CHECK (week_end <= '2026-09-18'),
  ticker TEXT NOT NULL,
  long_vol REAL,
  short_vol REAL,
  long_std_vol REAL,
  long_neg_vol REAL,
  short_std_vol REAL,
  short_neg_vol REAL,
  issue_type TEXT,
  PRIMARY KEY (week_end, ticker)
);

CREATE INDEX IF NOT EXISTS idx_jquants_weekly_margin_ticker
  ON jquants_weekly_margin(ticker, week_end);

-- Daily balances for the limited set of 日々公表銘柄. This source includes the
-- exchange's margin-regulation classification and remains distinct from both
-- the legacy weekly all-issues series and its daily all-issues replacement.
CREATE TABLE IF NOT EXISTS jquants_margin_alerts(
  publication_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  applied_date TEXT,
  publication_reason TEXT,
  short_outstanding REAL,
  short_change REAL,
  short_ratio REAL,
  long_outstanding REAL,
  long_change REAL,
  long_ratio REAL,
  short_long_ratio REAL,
  short_negotiable_outstanding REAL,
  short_negotiable_change REAL,
  short_standard_outstanding REAL,
  short_standard_change REAL,
  long_negotiable_outstanding REAL,
  long_negotiable_change REAL,
  long_standard_outstanding REAL,
  long_standard_change REAL,
  tse_margin_regulation_classification TEXT,
  PRIMARY KEY (publication_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_jquants_margin_alerts_ticker
  ON jquants_margin_alerts(ticker, publication_date);

-- All-issues daily balances beginning with the 2026-09-25 balance. A separate
-- table is a semantic boundary: these rows must never be read as weekly history.
CREATE TABLE IF NOT EXISTS jquants_all_issues_daily_margin(
  balance_date TEXT NOT NULL CHECK (balance_date >= '2026-09-25'),
  ticker TEXT NOT NULL,
  long_vol REAL,
  short_vol REAL,
  long_std_vol REAL,
  long_neg_vol REAL,
  short_std_vol REAL,
  short_neg_vol REAL,
  issue_type TEXT,
  PRIMARY KEY (balance_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_jquants_all_issues_daily_margin_ticker
  ON jquants_all_issues_daily_margin(ticker, balance_date);

-- Investor-level positions disclosed under the 0.5% short-position reporting
-- rule. Names remain the provider's exact reporter identity fields: guessing
-- corporate identity across spelling changes would turn source facts into an
-- entity-resolution estimate. Disclosure and calculation dates are separate so
-- a historical panel can enforce both sides of the point-in-time boundary.
CREATE TABLE IF NOT EXISTS jquants_short_sale_reports(
  disclosed_at TEXT NOT NULL,
  source_ordinal INTEGER NOT NULL,
  calculated_at TEXT NOT NULL,
  ticker TEXT NOT NULL,
  short_seller_name TEXT NOT NULL,
  discretionary_investment_contractor_name TEXT NOT NULL,
  investment_fund_name TEXT NOT NULL,
  short_ratio REAL,
  short_shares INTEGER,
  short_trading_units INTEGER,
  previous_reported_at TEXT,
  previous_short_ratio REAL,
  is_cancellation INTEGER NOT NULL,
  notes TEXT,
  PRIMARY KEY (disclosed_at, source_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_jquants_short_sale_reports_ticker
  ON jquants_short_sale_reports(ticker, disclosed_at, calculated_at);

CREATE TABLE IF NOT EXISTS edinet_documents(
  doc_date TEXT NOT NULL,
  sequence_number INTEGER NOT NULL,
  doc_id TEXT NOT NULL,
  sec_code TEXT,
  doc_type_code TEXT,
  csv_flag TEXT,
  xbrl_flag TEXT,
  legal_status TEXT,
  disclosure_status TEXT,
  withdrawal_status TEXT,
  doc_info_edit_status TEXT,
  parent_doc_id TEXT,
  operation_datetime TEXT,
  submit_datetime TEXT,
  doc_description TEXT,
  period_start TEXT,
  period_end TEXT,
  edinet_code TEXT,
  issuer_edinet_code TEXT,
  subject_edinet_code TEXT,
  PRIMARY KEY (doc_date, sequence_number)
);

CREATE TABLE IF NOT EXISTS edinet_document_lists(
  doc_date TEXT PRIMARY KEY,
  process_datetime TEXT,
  result_count INTEGER NOT NULL,
  fetched_at_utc TEXT NOT NULL,
  is_final INTEGER NOT NULL
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
  extractor_revision TEXT,
  source_document_revision TEXT,
  investment_securities REAL,
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

CREATE TABLE IF NOT EXISTS lake_store_origin(
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  release_id TEXT NOT NULL,
  release_manifest_sha256 TEXT NOT NULL
    CHECK (length(release_manifest_sha256) = 64)
);

CREATE TABLE IF NOT EXISTS tse_capital_policy_snapshots(
  snapshot_month_end TEXT NOT NULL,
  ticker TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('disclosed', 'considering')),
  status_change TEXT,
  updated_on TEXT,
  contact_requested INTEGER NOT NULL,
  first_disclosed_month_end TEXT,
  first_disclosure_left_censored INTEGER NOT NULL,
  PRIMARY KEY (snapshot_month_end, ticker)
);

CREATE INDEX IF NOT EXISTS idx_tse_capital_policy_snapshots_ticker
  ON tse_capital_policy_snapshots(ticker, snapshot_month_end);

CREATE TABLE IF NOT EXISTS jpx_delistings(
  delisted_on TEXT NOT NULL,
  ticker TEXT NOT NULL,
  name TEXT NOT NULL,
  market TEXT,
  reason TEXT NOT NULL,
  PRIMARY KEY (delisted_on, ticker)
);

CREATE TABLE IF NOT EXISTS tender_offer_exit_values(
  ticker TEXT NOT NULL,
  delisted_on TEXT NOT NULL,
  offer_price_yen REAL NOT NULL CHECK (offer_price_yen > 0),
  offer_doc_id TEXT NOT NULL,
  result_doc_id TEXT NOT NULL,
  filed_on TEXT NOT NULL,
  PRIMARY KEY (ticker, delisted_on)
);
"""


class SQLiteSchemaError(RuntimeError):
    """Raised when an existing SQLite cache is not the current schema."""


def open_connection(db_path: Path) -> sqlite3.Connection:
    """Open a current cache or create a fresh one; reject every obsolete shape."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        if _existing_tables(conn):
            _upgrade_existing_store(conn)
            validate_current_schema(conn)
        else:
            conn.executescript(_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


def _upgrade_existing_store(conn: sqlite3.Connection) -> None:
    """Accept only the current store contract."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if current == SQLITE_SCHEMA_VERSION:
        return
    raise SQLiteSchemaError(
        "obsolete market SQLite schema; replace it with a current local build "
        f"(found user_version={current}, expected={SQLITE_SCHEMA_VERSION})"
    )


def connect_current(sqlite_path: Path) -> sqlite3.Connection | None:
    """Open an existing store only when it already matches the current schema.

    Returns `None` for a missing/legacy/corrupt file so read helpers degrade to
    "cache cannot serve this" rather than raising, letting bootstrap/fetch
    commands populate the missing coverage.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(sqlite_path)
        validate_current_schema(conn)
    except (SQLiteSchemaError, sqlite3.Error):
        if conn is not None:
            conn.close()
        return None
    return conn


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _validate_current_schema(conn: sqlite3.Connection, tables: set[str]) -> None:
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if user_version != SQLITE_SCHEMA_VERSION:
        raise SQLiteSchemaError(
            "obsolete market SQLite schema; replace it with a current local build "
            f"(found user_version={user_version}, expected={SQLITE_SCHEMA_VERSION})"
        )
    missing = sorted(set(_REQUIRED_TABLES) - tables)
    if missing:
        raise SQLiteSchemaError(
            "market SQLite schema is incomplete; replace it with a current local build "
            f"(missing tables: {', '.join(missing)})"
        )
    expected_tables, expected_indexes = _expected_schema_shape()
    for table, required_columns in _REQUIRED_COLUMNS.items():
        existing_info = _table_info(conn, table)
        existing_columns = {column[0] for column in existing_info}
        missing_columns = sorted(set(required_columns) - existing_columns)
        if missing_columns:
            raise SQLiteSchemaError(
                "market SQLite schema is incomplete; replace it with a current local build "
                f"(missing columns in {table}: {', '.join(missing_columns)})"
            )
        if existing_info != expected_tables[table]:
            raise SQLiteSchemaError(
                "market SQLite schema is incomplete; replace it with a current local build "
                f"(table shape mismatch: {table})"
            )
    for index_name, expected_index in expected_indexes.items():
        existing_index = _index_info(conn, expected_index[0], index_name)
        if existing_index != expected_index:
            raise SQLiteSchemaError(
                "market SQLite schema is incomplete; replace it with a current local build "
                f"(index shape mismatch: {index_name})"
            )


def validate_current_schema(conn: sqlite3.Connection) -> None:
    """Validate that an existing market SQLite connection has the current schema."""
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
