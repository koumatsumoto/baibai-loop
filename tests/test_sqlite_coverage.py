from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

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


def _verify_screening_sqlite_coverage(*args, **kwargs):
    with patch("baibai_loop.screening.sqlite_coverage._MIN_COMMON_STOCK_MASTER_ROWS", 100):
        return verify_screening_sqlite_coverage(*args, **kwargs)


def _add_raw_import(
    conn: sqlite3.Connection,
    *,
    source: str,
    path: str,
    record_count: int,
    min_date: str,
    max_date: str,
) -> None:
    fetched_at = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO raw_imports("
        "source, path, sha256, imported_at_utc, record_count, min_date, max_date"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            path,
            "0" * 64,
            fetched_at,
            record_count,
            min_date,
            max_date,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO source_coverage("
        "source, operation, coverage_key, coverage_start, coverage_end, "
        "requested_start, requested_end, params_json, fetched_at_utc, record_count"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            "test",
            path,
            min_date,
            max_date,
            min_date,
            max_date,
            "{}",
            fetched_at,
            record_count,
        ),
    )


def _populate_complete_coverage(conn: sqlite3.Connection, asof: date) -> None:
    bars_start = asof - timedelta(days=1200)
    fin_start = asof - timedelta(days=730)
    earnings_date = asof + timedelta(days=7)
    tickers = tuple(f"{1301 + index:04d}" for index in range(100))
    conn.executemany(
        "INSERT INTO jquants_master_snapshots("
        "snapshot_date, ticker, name, market, sector_33, is_common_stock, raw_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (asof.isoformat(), ticker, f"Name {ticker}", "Prime", "水産・農林業", 1, "{}")
            for ticker in tickers
        ],
    )
    _add_raw_import(
        conn,
        source="jquants_master_snapshots",
        path="records/_data/raw/screening/jquants/get_eq_master.json",
        record_count=len(tickers),
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    bar_rows: list[tuple[str, str, float, float]] = []
    current = bars_start
    while current <= asof:
        if current.weekday() < 5:
            bar_rows.extend(
                (ticker, current.isoformat(), 1000.0, 200_000_000.0) for ticker in tickers
            )
        current += timedelta(days=1)
    conn.executemany(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close, turnover_value) "
        "VALUES (?, ?, ?, ?)",
        bar_rows,
    )
    _add_raw_import(
        conn,
        source="jquants_daily_bars",
        path=(
            "records/_data/raw/screening/jquants/"
            f"get_eq_bars_daily_range-end_dt-{asof.isoformat()}-"
            f"start_dt-{bars_start.isoformat()}.json"
        ),
        record_count=len(bar_rows),
        min_date=bars_start.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.executemany(
        "INSERT INTO jquants_fin_summaries(ticker, disclosed_at, eps_ttm, raw_json) "
        "VALUES (?, ?, ?, ?)",
        [(ticker, asof.isoformat(), 100.0, "{}") for ticker in tickers],
    )
    _add_raw_import(
        conn,
        source="jquants_fin_summaries",
        path=(
            "records/_data/raw/screening/jquants/"
            f"get_fin_summary_range-end_dt-{asof.isoformat()}-"
            f"start_dt-{fin_start.isoformat()}.json"
        ),
        record_count=len(tickers),
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
        "INSERT INTO source_coverage("
        "source, operation, coverage_key, coverage_start, coverage_end, "
        "requested_start, requested_end, params_json, fetched_at_utc, record_count"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "jquants_earnings_calendar",
            "get_eq_earnings_cal",
            "whole-list",
            asof.isoformat(),
            (asof + timedelta(days=90)).isoformat(),
            asof.isoformat(),
            (asof + timedelta(days=90)).isoformat(),
            "{}",
            datetime.now(UTC).isoformat(),
            1,
        ),
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
    conn.execute(
        "INSERT INTO jpx_regulation_sources(asof_date, source_name, fetched_at_utc) "
        "VALUES (?, ?, ?)",
        (asof.isoformat(), "test-source", datetime.now(UTC).isoformat()),
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
            issues = _verify_screening_sqlite_coverage(
                Path(tmp) / "missing.sqlite",
                date(2026, 5, 8),
            )

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].source, "sqlite")

    def test_corrupt_sqlite_file_reports_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            sqlite_path.write_bytes(b"not a sqlite database")

            issues = _verify_screening_sqlite_coverage(sqlite_path, date(2026, 5, 8))

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].source, "sqlite")
            self.assertIn("SQLite coverage query failed", issues[0].reason)

    def test_complete_required_windows_has_no_issues(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(
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

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_daily_bars", {issue.source for issue in issues})

    def test_stale_earnings_calendar_horizon_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute(
                "UPDATE source_coverage SET coverage_end = ? WHERE source = ? AND coverage_key = ?",
                (
                    (asof + timedelta(days=30)).isoformat(),
                    "jquants_earnings_calendar",
                    "whole-list",
                ),
            )
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_earnings_calendar"
                    and "horizon is not covered" in issue.reason
                    for issue in issues
                )
            )

    def test_zero_row_earnings_calendar_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute("DELETE FROM jquants_earnings_calendar")
            conn.execute("DELETE FROM raw_imports WHERE source = ?", ("jquants_earnings_calendar",))
            conn.execute(
                "UPDATE source_coverage SET record_count = ? WHERE source = ?",
                (0, "jquants_earnings_calendar"),
            )
            _record_table_counts(conn)
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_earnings_calendar"
                    and "zero imported rows" in issue.reason
                    for issue in issues
                )
            )

    def test_source_coverage_status_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.commit()
            conn.close()
            conn = open_connection(sqlite_path)
            conn.execute(
                "UPDATE source_coverage SET status = ? WHERE source = ?",
                ("failed", "jquants_daily_bars"),
            )
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars" and "status is not ok" in issue.reason
                    for issue in issues
                )
            )

    def test_old_non_ok_source_coverage_outside_required_window_does_not_block(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            old_start = asof - timedelta(days=1300)
            old_end = asof - timedelta(days=1270)
            conn.execute(
                "INSERT INTO source_coverage("
                "source, operation, coverage_key, coverage_start, coverage_end, "
                "requested_start, requested_end, params_json, fetched_at_utc, record_count, status"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "jquants_daily_bars",
                    "get_eq_bars_daily_range",
                    "old-failed",
                    old_start.isoformat(),
                    old_end.isoformat(),
                    old_start.isoformat(),
                    old_end.isoformat(),
                    "{}",
                    datetime.now(UTC).isoformat(),
                    0,
                    "failed",
                ),
            )
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertFalse(
                any(
                    issue.source == "jquants_daily_bars" and "old-failed" in issue.requirement
                    for issue in issues
                ),
                issues,
            )

    def test_skipped_normalized_rows_are_observability_not_fail_fast(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.commit()
            conn.close()
            conn = open_connection(sqlite_path)
            conn.execute(
                "UPDATE source_coverage SET skipped_record_count = 1 WHERE source = ?",
                ("jquants_daily_bars",),
            )
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertFalse(
                any(issue.source == "jquants_daily_bars" for issue in issues),
                issues,
            )

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

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_master_snapshots", {issue.source for issue in issues})
            self.assertTrue(any("zero imported rows" in issue.reason for issue in issues))

    def test_tiny_common_stock_master_reports_incomplete_cache(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute(
                "UPDATE jquants_master_snapshots SET is_common_stock = 0 WHERE ticker != ?",
                ("1301",),
            )
            _record_table_counts(conn)
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_master_snapshots"
                    and "common-stock row count" in issue.reason
                    for issue in issues
                )
            )

    def test_master_below_production_floor_reports_incomplete_cache(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            tickers = tuple(f"{1301 + index:04d}" for index in range(2499))
            conn.executemany(
                "INSERT INTO jquants_master_snapshots("
                "snapshot_date, ticker, name, market, sector_33, is_common_stock, raw_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        asof.isoformat(),
                        ticker,
                        f"Name {ticker}",
                        "Prime",
                        "水産・農林業",
                        1,
                        "{}",
                    )
                    for ticker in tickers
                ],
            )
            _add_raw_import(
                conn,
                source="jquants_master_snapshots",
                path="records/_data/raw/screening/jquants/get_eq_master.json",
                record_count=len(tickers),
                min_date=asof.isoformat(),
                max_date=asof.isoformat(),
            )
            _record_table_counts(conn)
            conn.commit()
            conn.close()

            issues = verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_master_snapshots"
                    and "common-stock row count" in issue.reason
                    and "minimum 2500" in issue.reason
                    for issue in issues
                )
            )

    def test_production_floor_feeds_daily_density_check(self) -> None:
        asof = date(2026, 5, 8)
        bars_start = asof - timedelta(days=1200)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            tickers = tuple(f"{1301 + index:04d}" for index in range(2500))
            conn.executemany(
                "INSERT INTO jquants_master_snapshots("
                "snapshot_date, ticker, name, market, sector_33, is_common_stock, raw_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        asof.isoformat(),
                        ticker,
                        f"Name {ticker}",
                        "Prime",
                        "水産・農林業",
                        1,
                        "{}",
                    )
                    for ticker in tickers
                ],
            )
            _add_raw_import(
                conn,
                source="jquants_master_snapshots",
                path="records/_data/raw/screening/jquants/get_eq_master.json",
                record_count=len(tickers),
                min_date=asof.isoformat(),
                max_date=asof.isoformat(),
            )
            conn.executemany(
                "INSERT INTO jquants_daily_bars(ticker, traded_at, close, turnover_value) "
                "VALUES (?, ?, ?, ?)",
                [(ticker, asof.isoformat(), 1000.0, 200_000_000.0) for ticker in tickers[:100]],
            )
            _add_raw_import(
                conn,
                source="jquants_daily_bars",
                path=(
                    "records/_data/raw/screening/jquants/"
                    f"get_eq_bars_daily_range-end_dt-{asof.isoformat()}-"
                    f"start_dt-{bars_start.isoformat()}.json"
                ),
                record_count=100,
                min_date=bars_start.isoformat(),
                max_date=asof.isoformat(),
            )
            _record_table_counts(conn)
            conn.commit()
            conn.close()

            issues = verify_screening_sqlite_coverage(
                sqlite_path,
                asof,
                require_edinet_metrics=False,
                allow_stale_jpx=True,
            )

            self.assertFalse(
                any(
                    issue.source == "jquants_master_snapshots"
                    and "common-stock row count" in issue.reason
                    for issue in issues
                )
            )
            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars"
                    and "relative to common-stock master rows (2500)" in issue.reason
                    for issue in issues
                )
            )

    def test_table_count_below_raw_import_count_reports_incomplete_cache(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute("DELETE FROM jquants_daily_bars")
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars" and "row count" in issue.reason
                    for issue in issues
                )
            )

    def test_recent_daily_bars_sparse_history_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute(
                "DELETE FROM jquants_daily_bars WHERE traded_at < ?",
                (asof.isoformat(),),
            )
            _record_table_counts(conn)
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars" and "recent 30-day window" in issue.reason
                    for issue in issues
                )
            )

    def test_old_daily_bars_sparse_history_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            weak_start = (asof - timedelta(days=1200)) + timedelta(days=240)
            weak_end = weak_start + timedelta(days=119)
            conn.execute(
                "DELETE FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ?",
                (weak_start.isoformat(), weak_end.isoformat()),
            )
            _record_table_counts(conn)
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars" and "long-history" in issue.reason
                    for issue in issues
                )
            )

    def test_fin_summary_sparse_ticker_coverage_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute(
                "DELETE FROM jquants_fin_summaries WHERE ticker >= ?",
                ("1350",),
            )
            _record_table_counts(conn)
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_fin_summaries"
                    and "usable ticker count" in issue.reason
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

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

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

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

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

            issues = _verify_screening_sqlite_coverage(
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

    def test_stale_jpx_snapshot_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute(
                "UPDATE jpx_regulation_sources SET fetched_at_utc = ? WHERE asof_date = ?",
                ("2026-04-01T00:00:00+00:00", asof.isoformat()),
            )
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jpx_regulation_flags"
                    and "fetched_at_utc is stale" in issue.reason
                    for issue in issues
                )
            )

    def test_jpx_source_row_without_fetched_at_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute(
                "UPDATE jpx_regulation_sources SET fetched_at_utc = NULL WHERE asof_date = ?",
                (asof.isoformat(),),
            )
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jpx_regulation_flags"
                    and "without fetched_at_utc" in issue.reason
                    for issue in issues
                )
            )

    def test_allow_stale_jpx_suppresses_freshness_issue(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute(
                "UPDATE jpx_regulation_sources SET fetched_at_utc = ? WHERE asof_date = ?",
                ("2026-04-01T00:00:00+00:00", asof.isoformat()),
            )
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(
                sqlite_path,
                asof,
                allow_stale_jpx=True,
            )

            self.assertFalse(
                any(
                    issue.source == "jpx_regulation_flags"
                    and "fetched_at_utc is stale" in issue.reason
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

            issues = _verify_screening_sqlite_coverage(
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

            issues = _verify_screening_sqlite_coverage(
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

    def test_required_edinet_failed_status_reports_diagnostic_error(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, asof)
            conn.execute(
                "INSERT OR REPLACE INTO source_coverage("
                "source, operation, coverage_key, coverage_start, coverage_end, "
                "requested_start, requested_end, params_json, fetched_at_utc, record_count, "
                "status, error"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "edinet_metrics",
                    "extract_edinet_metrics",
                    asof.isoformat(),
                    asof.isoformat(),
                    asof.isoformat(),
                    asof.isoformat(),
                    asof.isoformat(),
                    "{}",
                    datetime.now(UTC).isoformat(),
                    1,
                    "failed",
                    "1 EDINET CSV hard failures",
                ),
            )
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "edinet_metrics"
                    and "1 EDINET CSV hard failures" in issue.reason
                    for issue in issues
                )
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
