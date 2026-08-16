"""Forward-only schema migrations for the single physical store (`market.sqlite`).

The store holds gigabytes of API-rate-limited price/fundamentals cache, so a
schema change upgrades an existing file in place instead of forcing a full
re-fetch. Fifteen of its tables are refilled from the L1 release, but the store is
the shape ingest writes into and the four tables the lake does not own are only
here, so the file is migrated rather than rebuilt.
`BASELINE_VERSION` is the oldest `user_version` the migration path accepts: a
store at exactly the baseline (or any later version below `LATEST_VERSION`) is
migrated forward one step at a time; an older store is rejected fail-fast because
no forward path reconstructs it. `LATEST_VERSION` is the current schema version
and equals `SQLITE_SCHEMA_VERSION`; a fresh store is created directly at the
latest DDL and never replays migrations.

To evolve the schema: add a `Migration` with the next `version` (contiguous, no
gaps) and bump `SQLITE_SCHEMA_VERSION` in `schema.py` to match. Additive changes
that append a column at the DDL tail may use a plain `ALTER TABLE ... ADD COLUMN`
statement. A change that reorders or drops columns must instead rebuild the table
via `rebuild_table` in the migration's `transform` hook, because the strict shape
check compares column order against the DDL and an appended column would fail it.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# The oldest schema version the forward-migration path accepts. A store below
# this is rejected (delete-and-rebuild); a store between here and LATEST_VERSION
# is migrated up. This is the version at which version-managed migrations begin.
BASELINE_VERSION = 13

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward step from `version - 1` to `version`.

    `statements` are plain SQL run in order; `transform` is an optional procedural
    hook (e.g. one calling `rebuild_table`) run after them in the same transaction.
    """

    version: int
    statements: tuple[str, ...] = ()
    transform: Callable[[sqlite3.Connection], None] | None = None


def _migrate_v14_edinet_document_events(conn: sqlite3.Connection) -> None:
    """Replace the rebuildable EDINET list cache with event-complete storage."""
    conn.execute("DROP TABLE edinet_documents")
    conn.execute(
        """
        CREATE TABLE edinet_documents(
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
          PRIMARY KEY (doc_date, sequence_number)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE edinet_document_lists(
          doc_date TEXT PRIMARY KEY,
          process_datetime TEXT,
          result_count INTEGER NOT NULL,
          fetched_at_utc TEXT NOT NULL,
          is_final INTEGER NOT NULL
        )
        """
    )
    conn.execute("DELETE FROM source_coverage WHERE source = 'edinet_documents'")


def _migrate_v21_margin_publication_domains(conn: sqlite3.Connection) -> None:
    """Put the legacy weekly date boundary below every write path, including merge."""
    # SQLite keeps explicitly named indexes attached to the renamed old table;
    # release the name before `rebuild_table` creates the replacement index.
    conn.execute("DROP INDEX IF EXISTS idx_jquants_weekly_margin_ticker")
    rebuild_table(
        conn,
        table="jquants_weekly_margin",
        create_statements=(
            """
            CREATE TABLE jquants_weekly_margin(
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
            )
            """,
            (
                "CREATE INDEX idx_jquants_weekly_margin_ticker "
                "ON jquants_weekly_margin(ticker, week_end)"
            ),
        ),
        columns=(
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
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=14, transform=_migrate_v14_edinet_document_events),
    Migration(
        version=15,
        statements=(
            "ALTER TABLE edinet_metrics ADD COLUMN extractor_revision TEXT",
            "ALTER TABLE edinet_metrics ADD COLUMN source_document_revision TEXT",
        ),
    ),
    Migration(
        version=16,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS jquants_weekly_margin(
              week_end TEXT NOT NULL,
              ticker TEXT NOT NULL,
              long_vol REAL,
              short_vol REAL,
              long_std_vol REAL,
              long_neg_vol REAL,
              short_std_vol REAL,
              short_neg_vol REAL,
              issue_type TEXT,
              PRIMARY KEY (week_end, ticker)
            )
            """,
            (
                "CREATE INDEX IF NOT EXISTS idx_jquants_weekly_margin_ticker "
                "ON jquants_weekly_margin(ticker, week_end)"
            ),
        ),
    ),
    Migration(
        version=17,
        statements=("ALTER TABLE edinet_metrics ADD COLUMN investment_securities REAL",),
    ),
    Migration(
        version=18,
        statements=(
            "ALTER TABLE jquants_fin_summaries ADD COLUMN treasury_shares REAL",
            "ALTER TABLE jquants_fin_summaries ADD COLUMN equity_to_asset_ratio REAL",
        ),
    ),
    Migration(
        version=19,
        statements=(
            # 自己株券買付状況報告書の毎月の状態。E[r] の carry は過去 1 年の株数変化なので、
            # 「これから何株買う権限が残っているか」はそこからは分からない。1 銘柄 1 報告月で
            # 持つのは、様式がその粒度で出るためである。
            """
            CREATE TABLE IF NOT EXISTS edinet_buyback_reports(
              ticker TEXT NOT NULL,
              report_month_end TEXT NOT NULL,
              doc_id TEXT NOT NULL,
              filed_on TEXT NOT NULL,
              window_start TEXT,
              window_end TEXT,
              resolved_shares INTEGER,
              resolved_amount_yen INTEGER,
              cumulative_shares INTEGER,
              cumulative_amount_yen INTEGER,
              month_shares INTEGER,
              month_amount_yen INTEGER,
              issued_shares INTEGER,
              treasury_shares INTEGER,
              PRIMARY KEY (ticker, report_month_end)
            )
            """,
            (
                "CREATE INDEX IF NOT EXISTS idx_edinet_buyback_reports_ticker "
                "ON edinet_buyback_reports(ticker, report_month_end)"
            ),
        ),
    ),
    Migration(
        version=20,
        statements=(
            """
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
            )
            """,
            (
                "CREATE INDEX IF NOT EXISTS idx_jquants_short_sale_reports_ticker "
                "ON jquants_short_sale_reports(ticker, disclosed_at, calculated_at)"
            ),
        ),
    ),
    Migration(
        version=21,
        statements=(
            """
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
            )
            """,
            (
                "CREATE INDEX IF NOT EXISTS idx_jquants_margin_alerts_ticker "
                "ON jquants_margin_alerts(ticker, publication_date)"
            ),
            """
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
            )
            """,
            (
                "CREATE INDEX IF NOT EXISTS idx_jquants_all_issues_daily_margin_ticker "
                "ON jquants_all_issues_daily_margin(ticker, balance_date)"
            ),
        ),
        transform=_migrate_v21_margin_publication_domains,
    ),
    Migration(
        version=22,
        statements=(
            # The list refresh writes these for every row it stores from now on, and
            # `screening backfill-edinet-documents` restores them for days already in
            # the store. They stay nullable so that a day whose list predates the
            # columns reads as unobserved rather than as an absence of filings.
            "ALTER TABLE edinet_documents ADD COLUMN edinet_code TEXT",
            "ALTER TABLE edinet_documents ADD COLUMN issuer_edinet_code TEXT",
            "ALTER TABLE edinet_documents ADD COLUMN subject_edinet_code TEXT",
            """
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
            )
            """,
            (
                "CREATE INDEX IF NOT EXISTS idx_tse_capital_policy_snapshots_ticker "
                "ON tse_capital_policy_snapshots(ticker, snapshot_month_end)"
            ),
            """
            CREATE TABLE IF NOT EXISTS jpx_delistings(
              delisted_on TEXT NOT NULL,
              ticker TEXT NOT NULL,
              name TEXT NOT NULL,
              market TEXT,
              reason TEXT NOT NULL,
              PRIMARY KEY (delisted_on, ticker)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tender_offer_exit_values(
              ticker TEXT NOT NULL,
              delisted_on TEXT NOT NULL,
              offer_price_yen REAL NOT NULL CHECK (offer_price_yen > 0),
              offer_doc_id TEXT NOT NULL,
              result_doc_id TEXT NOT NULL,
              filed_on TEXT NOT NULL,
              PRIMARY KEY (ticker, delisted_on)
            )
            """,
        ),
    ),
    Migration(
        version=23,
        statements=(
            # A year whose per-share dividend straddles a split cannot be converted to
            # today's share basis with one factor, because each payment is stated on the
            # basis at its own record date. Holding the payments separately makes that
            # conversion possible, and the paid amount over the share count checks it
            # from a direction that does not use the per-share figures at all. All stay
            # nullable: the source omits them for part of the filings, and a refetch is
            # what fills them for days already stored.
            "ALTER TABLE jquants_fin_summaries ADD COLUMN dividend_q1 REAL",
            "ALTER TABLE jquants_fin_summaries ADD COLUMN dividend_interim REAL",
            "ALTER TABLE jquants_fin_summaries ADD COLUMN dividend_q3 REAL",
            "ALTER TABLE jquants_fin_summaries ADD COLUMN dividend_year_end REAL",
            "ALTER TABLE jquants_fin_summaries ADD COLUMN dividend_total_annual REAL",
            "ALTER TABLE jquants_fin_summaries ADD COLUMN average_shares REAL",
        ),
    ),
)

LATEST_VERSION = MIGRATIONS[-1].version if MIGRATIONS else BASELINE_VERSION


def rebuild_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    create_statements: Sequence[str],
    columns: Sequence[str],
) -> None:
    """Rebuild `table` under new DDL, copying `columns` from the old rows.

    The forward-only template for a change that reorders or drops columns:
    rename the existing table aside, create the new table (and its indexes) from
    `create_statements`, copy the retained `columns` across, then drop the old
    table. `columns` must exist in both the old and new table; a dropped column is
    simply omitted from `columns`.

    Runs inside the caller's migration transaction and issues only single
    statements (never `executescript`, which would commit that transaction).
    """
    if not _IDENTIFIER.match(table):
        raise ValueError(f"invalid table identifier: {table!r}")
    if not columns:
        raise ValueError("rebuild_table requires at least one carried column")
    for column in columns:
        if not _IDENTIFIER.match(column):
            raise ValueError(f"invalid column identifier: {column!r}")
    temp_table = f"_migrate_{table}"
    column_list = ", ".join(columns)
    # The interpolated names are migration-author constants validated against
    # _IDENTIFIER above; SQLite DDL cannot take identifiers as bind parameters.
    conn.execute(f"ALTER TABLE {table} RENAME TO {temp_table}")  # nosec B608
    for statement in create_statements:
        conn.execute(statement)
    conn.execute(
        f"INSERT INTO {table}({column_list}) SELECT {column_list} FROM {temp_table}"  # nosec B608
    )
    conn.execute(f"DROP TABLE {temp_table}")  # nosec B608


__all__ = [
    "BASELINE_VERSION",
    "LATEST_VERSION",
    "MIGRATIONS",
    "Migration",
    "rebuild_table",
]
