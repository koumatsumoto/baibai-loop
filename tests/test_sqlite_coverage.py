from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.sqlite_cache import open_connection
from baibai_loop.screening.sqlite_coverage import verify_screening_sqlite_coverage

_DATA_TABLES = (
    "jquants_daily_bars",
    "jquants_fin_summaries",
    "jquants_master_snapshots",
    "jquants_earnings_calendar",
    "jquants_market_calendar",
    "edinet_documents",
    "edinet_metrics",
    "jpx_regulation_flags",
    "jpx_regulation_sources",
)


def _add_raw_import(
    conn: sqlite3.Connection,
    *,
    source: str,
    path: str,
    record_count: int,
    min_date: str,
    max_date: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO raw_imports("
        "source, path, sha256, imported_at_utc, record_count, min_date, max_date"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            path,
            "0" * 64,
            datetime.now(UTC).isoformat(),
            record_count,
            min_date,
            max_date,
        ),
    )


def _populate_complete_coverage(conn: sqlite3.Connection, asof: date) -> None:
    bars_start = asof - timedelta(days=1200)
    fin_start = asof - timedelta(days=730)
    earnings_date = asof + timedelta(days=7)
    conn.execute(
        "INSERT INTO jquants_master_snapshots("
        "snapshot_date, ticker, name, market, sector_33, is_common_stock, raw_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (asof.isoformat(), "1301", "Kyokuyo", "Prime", "水産・農林業", 1, "{}"),
    )
    _add_raw_import(
        conn,
        source="jquants_master_snapshots",
        path="records/_data/raw/screening/jquants/get_eq_master.json",
        record_count=1,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.execute(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close, turnover_value) "
        "VALUES (?, ?, ?, ?)",
        ("1301", asof.isoformat(), 1000.0, 200_000_000.0),
    )
    _add_raw_import(
        conn,
        source="jquants_daily_bars",
        path=(
            "records/_data/raw/screening/jquants/"
            f"get_eq_bars_daily_range-end_dt-{asof.isoformat()}-"
            f"start_dt-{bars_start.isoformat()}.json"
        ),
        record_count=1,
        min_date=bars_start.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.execute(
        "INSERT INTO jquants_fin_summaries(ticker, disclosed_at, eps_ttm, raw_json) "
        "VALUES (?, ?, ?, ?)",
        ("1301", asof.isoformat(), 100.0, "{}"),
    )
    _add_raw_import(
        conn,
        source="jquants_fin_summaries",
        path=(
            "records/_data/raw/screening/jquants/"
            f"get_fin_summary_range-end_dt-{asof.isoformat()}-"
            f"start_dt-{fin_start.isoformat()}.json"
        ),
        record_count=1,
        min_date=fin_start.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.execute(
        "INSERT INTO jquants_earnings_calendar(announcement_date, ticker, raw_json) "
        "VALUES (?, ?, ?)",
        (
            earnings_date.isoformat(),
            "1301",
            f'{{"Code": "13010", "Date": "{earnings_date.isoformat()}"}}',
        ),
    )
    _add_raw_import(
        conn,
        source="jquants_earnings_calendar",
        path="records/_data/raw/screening/jquants/get_eq_earnings_cal.json",
        record_count=1,
        min_date=earnings_date.isoformat(),
        max_date=earnings_date.isoformat(),
    )
    conn.execute(
        "INSERT INTO jquants_market_calendar(day, is_business_day, raw_json) VALUES (?, ?, ?)",
        (asof.isoformat(), 1, "{}"),
    )
    _add_raw_import(
        conn,
        source="jquants_market_calendar",
        path=(
            "records/_data/raw/screening/jquants/"
            f"get_mkt_calendar-from_yyyymmdd-{asof:%Y%m%d}-to_yyyymmdd-{asof:%Y%m%d}.json"
        ),
        record_count=1,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    _add_raw_import(
        conn,
        source="jpx_regulation_flags",
        path=f"records/_data/raw/screening/jpx/regulations/{asof.isoformat()}.json",
        record_count=0,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    _add_raw_import(
        conn,
        source="edinet_metrics",
        path=f"records/_data/raw/screening/edinet/metrics/{asof.isoformat()}.json",
        record_count=1,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.execute(
        "INSERT INTO edinet_metrics(asof_date, ticker, sales_ttm) VALUES (?, ?, ?)",
        (asof.isoformat(), "1301", 1_000_000.0),
    )
    _record_table_counts(conn)


def _record_table_counts(conn: sqlite3.Connection) -> None:
    for table in _DATA_TABLES:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO cache_metadata(key, value) VALUES(?, ?)",
            (f"table_count.{table}", str(int(row[0] or 0))),
        )


class SQLiteCoverageTests(unittest.TestCase):
    def test_missing_sqlite_file_reports_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            issues = verify_screening_sqlite_coverage(
                Path(tmp) / "missing.sqlite",
                date(2026, 5, 8),
            )

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].source, "sqlite")

    def test_complete_required_windows_has_no_issues(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.commit()
            conn.close()

            issues = verify_screening_sqlite_coverage(
                sqlite_path,
                asof,
                require_edinet_metrics=True,
            )

            self.assertEqual(issues, ())

    def test_missing_daily_bars_reports_required_window(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute("DELETE FROM raw_imports WHERE source = ?", ("jquants_daily_bars",))
            conn.execute("DELETE FROM source_coverage WHERE source = ?", ("jquants_daily_bars",))
            conn.commit()
            conn.close()

            issues = verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_daily_bars", {issue.source for issue in issues})

    def test_zero_row_master_import_reports_incomplete_cache(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute("DELETE FROM raw_imports WHERE source = ?", ("jquants_master_snapshots",))
            conn.execute(
                "DELETE FROM source_coverage WHERE source = ?", ("jquants_master_snapshots",)
            )
            conn.execute("DELETE FROM jquants_master_snapshots")
            _add_raw_import(
                conn,
                source="jquants_master_snapshots",
                path="records/_data/raw/screening/jquants/get_eq_master.json",
                record_count=0,
                min_date=asof.isoformat(),
                max_date=asof.isoformat(),
            )
            _record_table_counts(conn)
            conn.commit()
            conn.close()

            issues = verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_master_snapshots", {issue.source for issue in issues})
            self.assertTrue(any("zero imported rows" in issue.reason for issue in issues))

    def test_table_count_below_raw_import_count_reports_incomplete_cache(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute("DELETE FROM jquants_daily_bars")
            conn.commit()
            conn.close()

            issues = verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars" and "row count" in issue.reason
                    for issue in issues
                )
            )

    def test_overlapping_daily_imports_do_not_trigger_unsafe_count_check(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            _add_raw_import(
                conn,
                source="jquants_daily_bars",
                path=(
                    "records/_data/raw/screening/jquants/"
                    f"get_eq_bars_daily_range-end_dt-{asof.isoformat()}-"
                    f"start_dt-{(asof - timedelta(days=1)).isoformat()}.json"
                ),
                record_count=1,
                min_date=asof.isoformat(),
                max_date=asof.isoformat(),
            )
            conn.commit()
            conn.close()

            issues = verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertFalse(
                any(
                    issue.source == "jquants_daily_bars" and "row count" in issue.reason
                    for issue in issues
                )
            )

    def test_market_calendar_requires_actual_asof_row(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute(
                "UPDATE jquants_market_calendar SET day = ? WHERE day = ?",
                ((asof - timedelta(days=1)).isoformat(), asof.isoformat()),
            )
            conn.commit()
            conn.close()

            issues = verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_market_calendar"
                    and "required date window" in issue.reason
                    for issue in issues
                )
            )

    def test_required_jpx_sources_must_be_present_in_snapshot(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.commit()
            conn.close()

            issues = verify_screening_sqlite_coverage(
                sqlite_path,
                asof,
                required_jpx_sources=("取引停止",),
            )

            self.assertTrue(
                any(
                    issue.source == "jpx_regulation_flags"
                    and "missing required JPX regulation sources" in issue.reason
                    for issue in issues
                )
            )

    def test_orphaned_jpx_source_rows_do_not_satisfy_raw_import_coverage(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute("DELETE FROM raw_imports WHERE source = ?", ("jpx_regulation_flags",))
            conn.execute("DELETE FROM source_coverage WHERE source = ?", ("jpx_regulation_flags",))
            conn.execute(
                "INSERT INTO jpx_regulation_sources(asof_date, source_name, fetched_at_utc) "
                "VALUES (?, ?, ?)",
                (asof.isoformat(), "取引停止", None),
            )
            _record_table_counts(conn)
            conn.commit()
            conn.close()

            issues = verify_screening_sqlite_coverage(
                sqlite_path,
                asof,
                required_jpx_sources=("取引停止",),
            )

            self.assertTrue(
                any(
                    issue.source == "jpx_regulation_flags"
                    and "source coverage is not covered" in issue.reason
                    for issue in issues
                )
            )

    def test_required_edinet_corruption_reports_issue_without_traceback(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute(
                "UPDATE edinet_metrics SET failure_reasons = ? WHERE asof_date = ?",
                ("not-json", asof.isoformat()),
            )
            conn.commit()
            conn.close()

            issues = verify_screening_sqlite_coverage(
                sqlite_path,
                asof,
                require_edinet_metrics=True,
            )

            self.assertTrue(
                any(
                    issue.source == "edinet_metrics" and "coverage query failed" in issue.reason
                    for issue in issues
                )
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
