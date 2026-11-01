"""Current-schema DDL and shape validation for the rebuildable market cache.

This is the single physical store (`market.sqlite`): the market price/calendar
tables, financial/disclosure projections and retained L1 receipts share one file, one
schema version, and one connection path. Each owner declares its table semantics;
this module composes the physical schema and validates its shape without choosing
models, candidates, or publication policy.

The cache is rebuilt or replaced explicitly when this contract changes. Runtime
readers never interpret or migrate an older shape.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

from baibai_engine.data_collection.providers.edinet.contract import EDINET_LIST_RESPONSES_DDL
from baibai_engine.data_collection.providers.edinet.storage_contracts import (
    EDINET_SOURCE_ARCHIVES_DDL,
)
from baibai_engine.data_collection.providers.jpx.contract import JPX_SOURCE_BUNDLES_DDL
from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.data_collection.providers.tradingview.contract import (
    TRADINGVIEW_SOURCE_RECEIPTS_DDL,
)
from baibai_engine.data_collection.receipts.articles.storage_contracts import ARTICLE_RECEIPTS_DDL
from baibai_engine.data_collection.receipts.public_pages.contract import (
    EQUITY_WEB_SOURCE_RESPONSES_DDL,
)
from baibai_engine.machine_analysis.observations.articles.storage_contracts import (
    ARTICLE_OBSERVATIONS_DDL,
)
from baibai_engine.machine_analysis.observations.disclosures.storage_contracts import (
    EDINET_DOCUMENT_LISTS_DDL,
    EDINET_DOCUMENTS_DDL,
    JPX_DELISTINGS_DDL,
    JPX_EARNINGS_CALENDAR_DDL,
    JPX_REGULATION_FLAGS_DDL,
    JPX_REGULATION_SOURCES_DDL,
    TENDER_OFFER_EXIT_VALUES_DDL,
    TSE_CAPITAL_POLICY_SNAPSHOTS_DDL,
)
from baibai_engine.machine_analysis.observations.external_assessments.contract import (
    ANALYST_FORECAST_DDL,
    ANALYST_RATING_DDL,
    MODEL_VALUATION_DDL,
    QUANTS_SCORE_DDL,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.contract import (
    DDL as TV_DDL,
)
from baibai_engine.machine_analysis.observations.financials.storage_contracts import (
    EDINET_DEBT_SCHEDULE_DDL,
    EDINET_METRICS_DDL,
    EDINET_SEGMENT_FACTS_DDL,
    JQUANTS_FIN_SUMMARIES_DDL,
)
from baibai_engine.machine_analysis.observations.prices.storage_contracts import (
    JQUANTS_DAILY_BARS_DDL,
    JQUANTS_MARKET_CALENDAR_DDL,
)
from baibai_engine.machine_analysis.observations.stocks.storage_contracts import (
    JQUANTS_MASTER_SNAPSHOTS_DDL,
)
from baibai_engine.machine_analysis.observations.trading_activity.storage_contracts import (
    JQUANTS_ALL_ISSUES_DAILY_MARGIN_DDL,
    JQUANTS_MARGIN_ALERTS_DDL,
    JQUANTS_SHORT_SALE_REPORTS_DDL,
    JQUANTS_WEEKLY_MARGIN_DDL,
)
from baibai_engine.storage.sqlite.source_cache.storage_contracts import (
    LAKE_STORE_ORIGIN_DDL,
    SOURCE_COVERAGE_DDL,
)

SQLITE_SCHEMA_VERSION = 49
SCHEMA_VERSION = str(SQLITE_SCHEMA_VERSION)

# EDINET list projection reconstructs the current rows from retained source receipts.
# A later response can omit fields after the public-inspection period; historical carry
# comes from earlier receipts for the same day, never from previously derived L2 rows.
# These indexes are part of the required read contract. Other indexes remain
# performance choices in the DDL; do not broaden the acceptance contract implicitly.
_REQUIRED_INDEX_NAMES = (
    "idx_jquants_daily_bars_traded_at",
    "idx_source_coverage_source_window",
    "idx_edinet_list_responses_date_received",
)

_SCHEMA_SQL = (
    JQUANTS_DAILY_BARS_DDL
    + JQUANTS_FIN_SUMMARIES_DDL
    + JQUANTS_MASTER_SNAPSHOTS_DDL
    + JPX_EARNINGS_CALENDAR_DDL
    + JQUANTS_MARKET_CALENDAR_DDL
    + JQUANTS_WEEKLY_MARGIN_DDL
    + JQUANTS_MARGIN_ALERTS_DDL
    + JQUANTS_ALL_ISSUES_DAILY_MARGIN_DDL
    + JQUANTS_SHORT_SALE_REPORTS_DDL
    + EDINET_DOCUMENTS_DDL
    + EDINET_DOCUMENT_LISTS_DDL
    + EDINET_SEGMENT_FACTS_DDL
    + EDINET_DEBT_SCHEDULE_DDL
    + EDINET_SOURCE_ARCHIVES_DDL
    + EDINET_METRICS_DDL
    + JPX_REGULATION_FLAGS_DDL
    + JPX_REGULATION_SOURCES_DDL
    + SOURCE_COVERAGE_DDL
    + LAKE_STORE_ORIGIN_DDL
    + TSE_CAPITAL_POLICY_SNAPSHOTS_DDL
    + JPX_DELISTINGS_DDL
    + TENDER_OFFER_EXIT_VALUES_DDL
    + TV_DDL
    + MODEL_VALUATION_DDL
    + ANALYST_RATING_DDL
    + QUANTS_SCORE_DDL
    + ANALYST_FORECAST_DDL
    + JQUANTS_SOURCE_PAGES_DDL
    + EDINET_LIST_RESPONSES_DDL
    + JPX_SOURCE_BUNDLES_DDL
    + TRADINGVIEW_SOURCE_RECEIPTS_DDL
    + EQUITY_WEB_SOURCE_RESPONSES_DDL
    + ARTICLE_RECEIPTS_DDL
    + ARTICLE_OBSERVATIONS_DDL
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
