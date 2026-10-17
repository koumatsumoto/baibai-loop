"""Current-schema DDL and shape validation for the rebuildable market cache.

This is the single physical store (`market.sqlite`): the market price/calendar
tables and the screening fundamentals/regulation tables share one file, one
schema version, and one connection path. Storage observations owns the physical
schema; market and screening readers share it without importing each other's
composition or acquisition services.

The cache is rebuilt or replaced explicitly when this contract changes. Runtime
readers never interpret or migrate an older shape.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

from baibai_engine.collection.edinet.contract import EDINET_LIST_RESPONSES_DDL
from baibai_engine.collection.jquants_contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.external_assessment.observations.contract import (
    ANALYST_RATING_DDL,
    MODEL_VALUATION_DDL,
    QUANTS_SCORE_DDL,
)
from baibai_engine.external_assessment.tradingview.contract import DDL as TV_DDL

SQLITE_SCHEMA_VERSION = 41
SCHEMA_VERSION = str(SQLITE_SCHEMA_VERSION)

# EDINET serves a filing's descriptive columns only while its public-inspection period
# runs. Once that period ends the day's list still returns the entry with these five
# nulled — measured over the whole store, a row that has run out carries none of them,
# while `doc_type_code`, `submit_datetime` and `doc_description` are present on every row
# that has not. The converse does not hold for the other two: a filing can genuinely have
# no securities code and no parent, so a null there is not by itself evidence of expiry.
# The company, form, timestamp and title a filing carried do not stop being true when
# they stop being served, so a store that once observed them keeps them — the ingest
# preserves it when the later response is null and the same document occupies the same
# list position. A new non-null provider value replaces the stored value. These document
# facts travel through L1; the machine-store merge does not merge document rows.
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
# different moments can differ here, so ingest takes the new response rather than
# preserving a previous lifecycle value when a field becomes null.
EDINET_DOCUMENT_LIFECYCLE_COLUMNS: tuple[str, ...] = (
    "legal_status",
    "withdrawal_status",
    "csv_flag",
    "xbrl_flag",
)

# These indexes are part of the required read contract. Other indexes remain
# performance choices in the DDL; do not broaden the acceptance contract implicitly.
_REQUIRED_INDEX_NAMES = (
    "idx_jquants_daily_bars_traded_at",
    "idx_source_coverage_source_window",
    "idx_edinet_list_responses_date_received",
)

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
  adjustment_factor REAL,
  upper_limit TEXT,
  lower_limit TEXT,
  ex_right_type TEXT,
  market_cap_million_yen REAL,
  PRIMARY KEY (ticker, traded_at)
);

CREATE INDEX IF NOT EXISTS idx_jquants_daily_bars_traded_at
  ON jquants_daily_bars(traded_at);

CREATE TABLE IF NOT EXISTS jquants_fin_summaries(
  ticker TEXT NOT NULL,
  disclosed_at TEXT NOT NULL,
  disclosure_number TEXT NOT NULL,
  document_payload TEXT NOT NULL,
  PRIMARY KEY (ticker, disclosed_at, disclosure_number)
);

CREATE TABLE IF NOT EXISTS jquants_master_snapshots(
  snapshot_date TEXT NOT NULL,
  security_code TEXT NOT NULL,
  record_json TEXT NOT NULL,
  PRIMARY KEY (snapshot_date, security_code)
);

CREATE TABLE IF NOT EXISTS jpx_earnings_calendar(
  announcement_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  PRIMARY KEY (announcement_date, ticker)
);

CREATE TABLE IF NOT EXISTS jquants_market_calendar(
  day TEXT PRIMARY KEY,
  holiday_division TEXT NOT NULL
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
  publication_date TEXT,
  long_value REAL,
  short_value REAL,
  long_std_value REAL,
  long_neg_value REAL,
  short_std_value REAL,
  short_neg_value REAL,
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
  notes TEXT,
  short_seller_address TEXT,
  discretionary_investment_contractor_address TEXT,
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
  is_final INTEGER NOT NULL,
  response_json TEXT
);

CREATE TABLE IF NOT EXISTS edinet_segment_facts(
  ticker TEXT NOT NULL,
  source_doc_id TEXT NOT NULL,
  source_submit_datetime TEXT NOT NULL,
  disclosed_on TEXT NOT NULL,
  source_element TEXT NOT NULL,
  source_context TEXT NOT NULL,
  issuer_id TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT NOT NULL,
  consolidation_basis TEXT NOT NULL,
  segment_axis TEXT NOT NULL,
  segment_key TEXT NOT NULL,
  segment_name TEXT,
  segment_kind TEXT NOT NULL,
  metric TEXT NOT NULL,
  profit_basis TEXT,
  value REAL,
  currency TEXT NOT NULL,
  source_locator TEXT NOT NULL,
  PRIMARY KEY (source_doc_id, source_context, source_element)
);

CREATE TABLE IF NOT EXISTS edinet_debt_schedule(
  ticker TEXT NOT NULL,
  source_doc_id TEXT NOT NULL,
  source_submit_datetime TEXT NOT NULL,
  disclosed_on TEXT NOT NULL,
  source_element TEXT NOT NULL,
  source_context TEXT NOT NULL,
  issuer_id TEXT NOT NULL,
  balance_sheet_date TEXT NOT NULL,
  consolidation_basis TEXT NOT NULL,
  debt_category TEXT NOT NULL,
  due_from_months INTEGER NOT NULL,
  due_to_months INTEGER NOT NULL,
  principal REAL,
  currency TEXT NOT NULL,
  source_locator TEXT NOT NULL,
  PRIMARY KEY (source_doc_id, balance_sheet_date, consolidation_basis,
               debt_category, due_from_months, due_to_months)
);

CREATE TABLE IF NOT EXISTS edinet_source_archives(
  doc_id TEXT NOT NULL,
  source_type INTEGER NOT NULL,
  stored_on TEXT NOT NULL,
  zip_content BLOB NOT NULL,
  PRIMARY KEY (doc_id, source_type)
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
  ocf_receivables_cash_effect REAL,
  ocf_inventories_cash_effect REAL,
  ocf_payables_cash_effect REAL,
  ocf_contract_liabilities_cash_effect REAL,
  ocf_advances_received_cash_effect REAL,
  ocf_other_payables_cash_effect REAL,
  capex_ppe_reported REAL,
  capex_intangible_reported REAL,
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


_SCHEMA_SQL += (
    TV_DDL
    + MODEL_VALUATION_DDL
    + ANALYST_RATING_DDL
    + QUANTS_SCORE_DDL
    + JQUANTS_SOURCE_PAGES_DDL
    + EDINET_LIST_RESPONSES_DDL
)


class SQLiteSchemaError(RuntimeError):
    """Raised when an existing SQLite cache is not the current schema."""


def open_connection(db_path: Path) -> sqlite3.Connection:
    """Open a current cache or create a fresh one; reject every obsolete shape."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        if _existing_tables(conn):
            validate_current_schema(conn)
        else:
            conn.executescript(
                "BEGIN IMMEDIATE;\n"
                + _SCHEMA_SQL
                + f"\nPRAGMA user_version = {SQLITE_SCHEMA_VERSION};\nCOMMIT;"
            )
        conn.commit()
        return conn
    except BaseException:
        conn.close()
        raise


def connect_current(sqlite_path: Path) -> sqlite3.Connection | None:
    """Open an existing current-schema store read-only; only absence is a cache miss."""
    if not sqlite_path.exists():
        return None
    conn = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        validate_current_schema(conn)
    except BaseException:
        conn.close()
        raise
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
    expected_tables, expected_indexes = _expected_schema_shape(_SCHEMA_SQL)
    missing = sorted(set(expected_tables) - tables)
    if missing:
        raise SQLiteSchemaError(
            "market SQLite schema is incomplete; replace it with a current local build "
            f"(missing tables: {', '.join(missing)})"
        )
    for table, expected_info in expected_tables.items():
        existing_info = _table_info(conn, table)
        existing_columns = {column[0] for column in existing_info}
        missing_columns = sorted({column[0] for column in expected_info} - existing_columns)
        if missing_columns:
            raise SQLiteSchemaError(
                "market SQLite schema is incomplete; replace it with a current local build "
                f"(missing columns in {table}: {', '.join(missing_columns)})"
            )
        if existing_info != expected_info:
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


@lru_cache(maxsize=1)
def _expected_schema_shape(
    schema_sql: str,
) -> tuple[
    dict[str, tuple[tuple[str, str, int, int], ...]],
    dict[str, tuple[str, tuple[str, ...], bool]],
]:
    # Only the static DDL-derived expectation is reused. Every real connection is
    # still inspected, so changing a database cannot reuse a prior acceptance.
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(schema_sql)
        tables = {
            table: _table_info(expected, table)
            for table in sorted(_existing_tables(expected))
            if not table.startswith("sqlite_")
        }
        indexes: dict[str, tuple[str, tuple[str, ...], bool]] = {}
        for index_name in _REQUIRED_INDEX_NAMES:
            row = expected.execute(
                "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            index_info = None if row is None else _index_info(expected, str(row[0]), index_name)
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
