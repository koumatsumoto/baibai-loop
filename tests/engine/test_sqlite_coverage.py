from __future__ import annotations

import io
import shutil
import sqlite3
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tests.helpers.master import insert_master_rows
from tests.helpers.screening_sqlite import (
    add_source_coverage as _add_source_coverage,
)
from tests.helpers.screening_sqlite import (
    insert_financial_record,
)

from baibai_engine.machine_analysis.screening.cli.cache import verify_cache_coverage_command
from baibai_engine.machine_analysis.screening.sqlite_coverage import (
    core as coverage_core,
)
from baibai_engine.machine_analysis.screening.sqlite_coverage import (
    jquants as jquants_coverage,
)
from baibai_engine.machine_analysis.screening.sqlite_coverage import (
    verify_screening_sqlite_coverage,
)
from baibai_engine.market.jquants_writer import (
    store_jquants_daily_bars,
    store_jquants_fin_summaries,
)
from baibai_engine.storage.observations.schema import open_connection


def _verify_screening_sqlite_coverage(*args, **kwargs):
    with patch("baibai_engine.market.master_snapshot.MIN_COMMON_STOCK_MASTER_ROWS", 100):
        return verify_screening_sqlite_coverage(*args, **kwargs)


_COMMON_COVERAGE_ASOF = date(2026, 5, 8)
_COMPLETE_COVERAGE_TEMPLATE_DIR: tempfile.TemporaryDirectory[str] | None = None
_COMPLETE_COVERAGE_TEMPLATE_PATH: Path | None = None


def _seed_complete_coverage(conn: sqlite3.Connection, asof: date) -> None:
    bars_start = asof - timedelta(days=2200)
    margin_week = (asof - timedelta(days=4)).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO jquants_weekly_margin("
        "week_end, ticker, long_vol, short_vol, long_std_vol, long_neg_vol, "
        "short_std_vol, short_neg_vol, issue_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (margin_week, "1301", 1.0, 1.0, 1.0, 0.0, 1.0, 0.0, "2"),
    )
    _add_source_coverage(
        conn,
        source="jquants_weekly_margin",
        coverage_key=f"get_mkt_margin_interest:{margin_week}..{margin_week}",
        record_count=1,
        min_date=margin_week,
        max_date=margin_week,
    )
    fin_start = asof - timedelta(days=2200)
    earnings_date = asof + timedelta(days=7)
    tickers = tuple(f"{1301 + index:04d}" for index in range(100))
    insert_master_rows(
        conn,
        [
            (asof.isoformat(), ticker, f"Name {ticker}", "Prime", "水産・農林業", "011")
            for ticker in tickers
        ],
    )
    _add_source_coverage(
        conn,
        source="jquants_master_snapshots",
        coverage_key=f"get_eq_master:{asof.isoformat()}..{asof.isoformat()}",
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
        "INSERT OR REPLACE INTO jquants_daily_bars(ticker, traded_at, close, turnover_value) "
        "VALUES (?, ?, ?, ?)",
        bar_rows,
    )
    _add_source_coverage(
        conn,
        source="jquants_daily_bars",
        coverage_key=f"get_eq_bars_daily_range:{bars_start.isoformat()}..{asof.isoformat()}",
        record_count=len(bar_rows),
        min_date=bars_start.isoformat(),
        max_date=asof.isoformat(),
    )
    for ticker in tickers:
        insert_financial_record(
            conn,
            {
                "Code": str(ticker) + "0",
                "DiscDate": asof.isoformat(),
                "EPS": 100.0,
                "ShOutFY": 10000000.0,
                "TrShFY": 1000000.0,
                "EqAR": 0.5,
                "DiscNo": asof.isoformat(),
            },
        )
    _add_source_coverage(
        conn,
        source="jquants_fin_summaries",
        coverage_key=f"get_fin_summary_range:{fin_start.isoformat()}..{asof.isoformat()}",
        record_count=len(tickers),
        min_date=fin_start.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.execute(
        "INSERT OR REPLACE INTO jpx_earnings_calendar(announcement_date, ticker) VALUES (?, ?)",
        (
            earnings_date.isoformat(),
            "1301",
        ),
    )
    _add_source_coverage(
        conn,
        source="jpx_earnings_calendar",
        coverage_key="get_earnings_calendar_snapshot:current",
        record_count=1,
        min_date=earnings_date.isoformat(),
        max_date=earnings_date.isoformat(),
    )
    conn.execute(
        "UPDATE source_coverage SET fetched_at_utc = ? WHERE source = ?",
        (f"{asof.isoformat()}T00:00:00+09:00", "jpx_earnings_calendar"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO jquants_market_calendar(day, holiday_division) VALUES (?, ?)",
        (asof.isoformat(), 1),
    )
    _add_source_coverage(
        conn,
        source="jquants_market_calendar",
        coverage_key=(f"get_mkt_calendar:{asof.isoformat()}..{asof.isoformat()}"),
        record_count=1,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    _add_source_coverage(
        conn,
        source="jpx_regulation_flags",
        coverage_key=f"jpx_regulations:{asof.isoformat()}",
        record_count=0,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.execute(
        "INSERT OR REPLACE INTO jpx_regulation_sources(asof_date, source_name, fetched_at_utc) "
        "VALUES (?, ?, ?)",
        (
            asof.isoformat(),
            "test-source",
            datetime.combine(asof, datetime.min.time(), UTC).isoformat(),
        ),
    )
    _add_source_coverage(
        conn,
        source="edinet_metrics",
        coverage_key=asof.isoformat(),
        record_count=1,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.execute(
        "INSERT OR REPLACE INTO edinet_metrics(asof_date, ticker, sales_ttm) VALUES (?, ?, ?)",
        (asof.isoformat(), "1301", 1_000_000.0),
    )


def _complete_coverage_template_path() -> Path:
    global _COMPLETE_COVERAGE_TEMPLATE_DIR, _COMPLETE_COVERAGE_TEMPLATE_PATH
    if _COMPLETE_COVERAGE_TEMPLATE_PATH is None:
        template_dir = tempfile.TemporaryDirectory()
        template_path = Path(template_dir.name) / "market.sqlite"
        with closing(open_connection(template_path)) as conn, conn:
            _seed_complete_coverage(conn, _COMMON_COVERAGE_ASOF)
            conn.commit()
        _COMPLETE_COVERAGE_TEMPLATE_DIR = template_dir
        _COMPLETE_COVERAGE_TEMPLATE_PATH = template_path
    return _COMPLETE_COVERAGE_TEMPLATE_PATH


@contextmanager
def _complete_coverage_database() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        sqlite_path = Path(tmp) / "market.sqlite"
        shutil.copyfile(_complete_coverage_template_path(), sqlite_path)
        yield sqlite_path


class StaleWeeklyMarginDoesNotStopTheRunTest(unittest.TestCase):
    """古い週次残高は任意軸の欠測であり、原本coverage不足へ格上げしない。"""

    _STALE_DAYS = 40

    def _store_with_a_stale_balance(self, tmp: str) -> Path:
        sqlite_path = Path(tmp) / "market.sqlite"
        shutil.copyfile(_complete_coverage_template_path(), sqlite_path)
        with closing(open_connection(sqlite_path)) as conn, conn:
            conn.execute("DELETE FROM jquants_weekly_margin")
            conn.execute("DELETE FROM source_coverage WHERE source = 'jquants_weekly_margin'")
            stale = (_COMMON_COVERAGE_ASOF - timedelta(days=self._STALE_DAYS)).isoformat()
            conn.execute(
                "INSERT INTO jquants_weekly_margin("
                "week_end, ticker, long_vol, short_vol, long_std_vol, long_neg_vol, "
                "short_std_vol, short_neg_vol, issue_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (stale, "1301", 1.0, 1.0, 1.0, 0.0, 1.0, 0.0, "2"),
            )
            _add_source_coverage(
                conn,
                source="jquants_weekly_margin",
                coverage_key=f"get_mkt_margin_interest:{stale}..{stale}",
                record_count=1,
                min_date=stale,
                max_date=stale,
            )
            conn.commit()
        return sqlite_path

    def test_verify_completes_without_naming_the_stale_balance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = self._store_with_a_stale_balance(tmp)
            stream = io.StringIO()
            with patch("baibai_engine.market.master_snapshot.MIN_COMMON_STOCK_MASTER_ROWS", 100):
                code = verify_cache_coverage_command(
                    sqlite_path=sqlite_path,
                    asof_date=_COMMON_COVERAGE_ASOF,
                    stdout=stream,
                )

        self.assertEqual(code, 0, stream.getvalue())
        self.assertIn("coverage complete", stream.getvalue())
        self.assertNotIn("weekly_margin", stream.getvalue())


def tearDownModule() -> None:
    global _COMPLETE_COVERAGE_TEMPLATE_DIR, _COMPLETE_COVERAGE_TEMPLATE_PATH
    if _COMPLETE_COVERAGE_TEMPLATE_DIR is not None:
        _COMPLETE_COVERAGE_TEMPLATE_DIR.cleanup()
        _COMPLETE_COVERAGE_TEMPLATE_DIR = None
        _COMPLETE_COVERAGE_TEMPLATE_PATH = None


class SQLiteCoverageTests(unittest.TestCase):
    def test_complete_coverage_clones_are_isolated(self) -> None:
        with (
            _complete_coverage_database() as first_path,
            closing(sqlite3.connect(first_path)) as first,
            first,
        ):
            first.execute("DELETE FROM jquants_master_snapshots")
            first.commit()
            first_count = int(
                first.execute("SELECT COUNT(*) FROM jquants_master_snapshots").fetchone()[0]
            )

        with _complete_coverage_database() as second_path:
            with closing(sqlite3.connect(second_path)) as second, second:
                second_count = int(
                    second.execute("SELECT COUNT(*) FROM jquants_master_snapshots").fetchone()[0]
                )

            self.assertEqual(first_count, 0)
            self.assertEqual(second_count, 100)

    def test_two_master_dates_verify_independently_through_public_coverage_gate(self) -> None:
        first = date(2026, 5, 8)
        second = date(2026, 5, 15)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            with closing(open_connection(sqlite_path)) as conn, conn:
                _seed_complete_coverage(conn, first)
                _seed_complete_coverage(conn, second)

                bars_start = first - timedelta(days=2200)
                fin_start = first - timedelta(days=2200)
                conn.execute(
                    "DELETE FROM source_coverage WHERE source = ?", ("jquants_daily_bars",)
                )
                bars_count = int(
                    conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0]
                )
                _add_source_coverage(
                    conn,
                    source="jquants_daily_bars",
                    coverage_key=f"get_eq_bars_daily_range:{bars_start}..{second}",
                    record_count=bars_count,
                    min_date=bars_start.isoformat(),
                    max_date=second.isoformat(),
                )
                conn.execute(
                    "DELETE FROM source_coverage WHERE source = ?", ("jquants_fin_summaries",)
                )
                fin_count = int(
                    conn.execute("SELECT COUNT(*) FROM jquants_fin_summaries").fetchone()[0]
                )
                _add_source_coverage(
                    conn,
                    source="jquants_fin_summaries",
                    coverage_key=f"get_fin_summary_range:{fin_start}..{second}",
                    record_count=fin_count,
                    min_date=fin_start.isoformat(),
                    max_date=second.isoformat(),
                )
                earnings_range = conn.execute(
                    "SELECT MIN(announcement_date), MAX(announcement_date), COUNT(*) "
                    "FROM jpx_earnings_calendar"
                ).fetchone()
                conn.execute(
                    "UPDATE source_coverage SET coverage_start = ?, coverage_end = ?, "
                    "record_count = ? WHERE source = ?",
                    (*earnings_range, "jpx_earnings_calendar"),
                )
                conn.commit()

            self.assertEqual(_verify_screening_sqlite_coverage(sqlite_path, first), ())
            self.assertEqual(_verify_screening_sqlite_coverage(sqlite_path, second), ())

            with closing(open_connection(sqlite_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM jquants_master_snapshots WHERE snapshot_date = ? AND security_code = ?",
                    (first.isoformat(), "13010"),
                )
                conn.commit()

            first_issues = _verify_screening_sqlite_coverage(sqlite_path, first)
            second_issues = _verify_screening_sqlite_coverage(sqlite_path, second)
            self.assertTrue(
                any(issue.source == "jquants_master_snapshots" for issue in first_issues)
            )
            self.assertEqual(second_issues, ())

            with closing(open_connection(sqlite_path)) as conn, conn:
                insert_master_rows(
                    conn, [(first.isoformat(), "1301", "name-1301", "Prime", "水産・農林業", "011")]
                )
                conn.execute(
                    "DELETE FROM source_coverage WHERE source = ? AND coverage_key = ?",
                    (
                        "jquants_master_snapshots",
                        f"get_eq_master:{second.isoformat()}..{second.isoformat()}",
                    ),
                )
                conn.commit()

            first_issues = _verify_screening_sqlite_coverage(sqlite_path, first)
            second_issues = _verify_screening_sqlite_coverage(sqlite_path, second)
            self.assertEqual(first_issues, ())
            self.assertTrue(
                any(issue.source == "jquants_master_snapshots" for issue in second_issues)
            )

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

    def test_a_damaged_page_stops_the_run_before_any_coverage_question(self) -> None:
        """A store that opens but is damaged still has to be refused.

        The file-is-not-a-database case above never reaches the completeness check
        at all, so it says nothing about a store that opens and is then found bad.

        Which of the two refusals fires depends on where the damage lands and on
        the SQLite build: the check can answer something other than 'ok', or the
        read can raise before it answers. Both stop the run with one `sqlite`
        issue, and that — not the wording — is the property. Whether the check is
        the quick or the deep one is pinned by the test below.
        """
        with _complete_coverage_database() as sqlite_path:
            payload = bytearray(sqlite_path.read_bytes())
            page_size = int.from_bytes(payload[16:18], "big")
            # Overwrite a page well past the header so the file still opens.
            corrupt_at = page_size * 12
            payload[corrupt_at : corrupt_at + page_size] = b"\xff" * page_size
            sqlite_path.write_bytes(bytes(payload))

            issues = _verify_screening_sqlite_coverage(sqlite_path, date(2026, 5, 8))

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].source, "sqlite")
            self.assertTrue(
                any(
                    marker in issues[0].reason
                    for marker in ("quick_check failed", "SQLite coverage query failed")
                ),
                issues[0].reason,
            )

    def test_store_completeness_is_checked_with_quick_check_only(self) -> None:
        """Pin the less expensive quick check rather than relying on identical results.

        Reading the emitted SQL is what pins the choice: both pragmas answer 'ok'
        on a healthy store, so an assertion on the result would pass either way.
        """
        asof = date(2026, 5, 8)
        statements: list[str] = []
        real_connect = coverage_core._connect_readonly

        def _tracing_connect(path: Path) -> sqlite3.Connection:
            conn = real_connect(path)
            conn.set_trace_callback(statements.append)
            return conn

        with (
            _complete_coverage_database() as sqlite_path,
            patch.object(coverage_core, "_connect_readonly", _tracing_connect),
        ):
            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

        self.assertEqual(issues, ())
        pragmas = [
            line for line in statements if "quick_check" in line or "integrity_check" in line
        ]
        self.assertEqual(pragmas, ["PRAGMA quick_check"])

    def test_complete_required_windows_has_no_issues(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertEqual(issues, ())

    def test_reported_missing_values_are_not_acquisition_gaps(self) -> None:
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE jquants_fin_summaries SET document_payload = json_set("
                    "document_payload, '$.ShOutFY', NULL, '$.TrShFY', NULL, '$.EqAR', NULL)"
                )
            self.assertEqual(
                _verify_screening_sqlite_coverage(sqlite_path, _COMMON_COVERAGE_ASOF), ()
            )

    def test_current_bar_window_cannot_satisfy_normalized_split_basis(self) -> None:
        asof = date(2026, 5, 8)
        current_start = asof - timedelta(days=1200)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM jquants_daily_bars WHERE traded_at < ?",
                    (current_start.isoformat(),),
                )
                conn.execute(
                    "UPDATE source_coverage SET coverage_start = ? "
                    "WHERE source = 'jquants_daily_bars'",
                    (current_start.isoformat(),),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.requirement.startswith("normalized_per_3fy split basis")
                    for issue in issues
                )
            )
            self.assertFalse(
                any(
                    issue.reason == "daily bars request window is not fully covered in SQLite"
                    for issue in issues
                )
            )

    def test_normalized_split_basis_rejects_sparse_old_cross_section(self) -> None:
        asof = date(2026, 5, 8)
        normalized_start = asof - timedelta(days=2200)
        current_start = asof - timedelta(days=1200)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM jquants_daily_bars WHERE traded_at < ? AND ticker != '1301'",
                    (current_start.isoformat(),),
                )
                actual_count = int(
                    conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0]
                )
                conn.execute(
                    "UPDATE source_coverage SET record_count = ? "
                    "WHERE source = 'jquants_daily_bars'",
                    (actual_count,),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.requirement == f"{normalized_start.isoformat()}..{asof.isoformat()}"
                    and "long-history usable date density is too small" in issue.reason
                    for issue in issues
                )
            )
            self.assertFalse(
                any(
                    issue.reason == "daily bars request window is not fully covered in SQLite"
                    for issue in issues
                )
            )

    def test_current_summary_window_cannot_satisfy_normalized_fy_history(self) -> None:
        asof = date(2026, 5, 8)
        current_start = asof - timedelta(days=730)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE source_coverage SET coverage_start = ? "
                    "WHERE source = 'jquants_fin_summaries'",
                    (current_start.isoformat(),),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.requirement.startswith("normalized_per_3fy FY history")
                    for issue in issues
                )
            )
            self.assertFalse(
                any(
                    issue.reason
                    == "financial summary request window is not fully covered in SQLite"
                    for issue in issues
                )
            )

    def test_missing_required_table_reports_schema_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute("DROP TABLE edinet_documents")
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].source, "sqlite")
            self.assertIn("missing tables: edinet_documents", issues[0].reason)

    def test_missing_required_column_reports_schema_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute("ALTER TABLE edinet_documents DROP COLUMN doc_description")
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].source, "sqlite")
            self.assertIn("missing columns in edinet_documents: doc_description", issues[0].reason)

    def test_required_table_without_primary_key_reports_schema_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                source_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'jquants_daily_bars'"
                ).fetchone()[0]
                without_primary_key = source_sql.replace(",\n  PRIMARY KEY (ticker, traded_at)", "")
                self.assertNotEqual(source_sql, without_primary_key)
                conn.execute("DROP TABLE jquants_daily_bars")
                conn.execute(without_primary_key)
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].source, "sqlite")
            self.assertIn("table shape mismatch: jquants_daily_bars", issues[0].reason)

    def test_missing_required_index_reports_schema_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute("DROP INDEX idx_jquants_daily_bars_traded_at")
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].source, "sqlite")
            self.assertIn(
                "index shape mismatch: idx_jquants_daily_bars_traded_at", issues[0].reason
            )

    def test_daily_bar_table_count_below_source_coverage_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute("DELETE FROM jquants_daily_bars WHERE ticker >= ?", ("1351",))
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_daily_bars", {issue.source for issue in issues})
            self.assertTrue(
                any(
                    "row count" in issue.reason and "source_coverage record_count" in issue.reason
                    for issue in issues
                )
            )

    def test_daily_bar_table_count_above_source_coverage_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE source_coverage SET record_count = ? WHERE source = ?",
                    (1, "jquants_daily_bars"),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_daily_bars", {issue.source for issue in issues})
            self.assertTrue(
                any(
                    "row count" in issue.reason and "source_coverage record_count" in issue.reason
                    for issue in issues
                )
            )

    def test_daily_bar_rows_outside_required_window_do_not_report_global_count_issue(
        self,
    ) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close, turnover_value) "
                    "VALUES (?, ?, ?, ?)",
                    ("1301", (asof + timedelta(days=1)).isoformat(), 1000.0, 200_000_000.0),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertFalse(
                any(
                    issue.source == "jquants_daily_bars"
                    and "row count" in issue.reason
                    and "source_coverage record_count" in issue.reason
                    for issue in issues
                )
            )

    def test_unrelated_receipt_windows_do_not_block_the_requested_run(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                for source in ("jquants_daily_bars", "jquants_fin_summaries"):
                    for day in (asof - timedelta(days=2300), asof + timedelta(days=30)):
                        _add_source_coverage(
                            conn,
                            source=source,
                            coverage_key=f"outside:{day}",
                            record_count=999,
                            min_date=day.isoformat(),
                            max_date=day.isoformat(),
                        )
            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)
        self.assertFalse(
            any(
                issue.requirement.startswith("outside:") and "row count" in issue.reason
                for issue in issues
            )
        )

    def test_normalization_history_receipts_remain_in_scope(self) -> None:
        asof = date(2026, 5, 8)
        older_input = asof - timedelta(days=2000)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                for source in ("jquants_daily_bars", "jquants_fin_summaries"):
                    _add_source_coverage(
                        conn,
                        source=source,
                        coverage_key="normalization-input",
                        record_count=999999,
                        min_date=older_input.isoformat(),
                        max_date=older_input.isoformat(),
                    )
            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)
        self.assertEqual(
            {
                issue.source
                for issue in issues
                if issue.requirement == "normalization-input" and "row count" in issue.reason
            },
            {"jquants_daily_bars", "jquants_fin_summaries"},
        )

    def test_daily_bar_required_window_count_mismatch_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM jquants_daily_bars WHERE ticker = ? AND traded_at = ?",
                    ("1301", asof.isoformat()),
                )
                conn.execute(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close, turnover_value) "
                    "VALUES (?, ?, ?, ?)",
                    ("9999", (asof + timedelta(days=1)).isoformat(), 1000.0, 200_000_000.0),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_daily_bars", {issue.source for issue in issues})
            self.assertTrue(
                any(
                    "row count in source_coverage window" in issue.reason
                    and "source_coverage record_count" in issue.reason
                    for issue in issues
                )
            )

    def test_daily_bar_reports_all_required_window_count_mismatches(self) -> None:
        asof = date(2026, 5, 8)
        previous_day = asof - timedelta(days=1)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                _add_source_coverage(
                    conn,
                    source="jquants_daily_bars",
                    coverage_key="window-a",
                    record_count=999,
                    min_date=previous_day.isoformat(),
                    max_date=previous_day.isoformat(),
                )
                _add_source_coverage(
                    conn,
                    source="jquants_daily_bars",
                    coverage_key="window-b",
                    record_count=999,
                    min_date=asof.isoformat(),
                    max_date=asof.isoformat(),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            mismatch_requirements = {
                issue.requirement
                for issue in issues
                if issue.source == "jquants_daily_bars"
                and "row count in source_coverage window" in issue.reason
            }
            self.assertGreaterEqual(mismatch_requirements, {"window-a", "window-b"})

    def test_fin_summary_table_count_below_source_coverage_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute("DELETE FROM jquants_fin_summaries WHERE ticker >= ?", ("1351",))
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_fin_summaries", {issue.source for issue in issues})
            self.assertTrue(
                any(
                    "row count" in issue.reason and "source_coverage record_count" in issue.reason
                    for issue in issues
                )
            )

    def test_fin_summary_table_count_above_source_coverage_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE source_coverage SET record_count = ? WHERE source = ?",
                    (1, "jquants_fin_summaries"),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_fin_summaries", {issue.source for issue in issues})
            self.assertTrue(
                any(
                    "row count" in issue.reason and "source_coverage record_count" in issue.reason
                    for issue in issues
                )
            )

    def test_fin_summary_required_window_count_mismatch_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM jquants_fin_summaries WHERE ticker = ? AND disclosed_at = ?",
                    ("1301", asof.isoformat()),
                )
                insert_financial_record(
                    conn,
                    {
                        "Code": "99990",
                        "DiscDate": (asof + timedelta(days=1)).isoformat(),
                        "EPS": 100.0,
                        "DiscNo": (asof + timedelta(days=1)).isoformat(),
                    },
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_fin_summaries", {issue.source for issue in issues})
            self.assertTrue(
                any(
                    "row count in source_coverage window" in issue.reason
                    and "source_coverage record_count" in issue.reason
                    for issue in issues
                )
            )

    def test_missing_daily_bars_reports_required_window(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM source_coverage WHERE source = ?", ("jquants_daily_bars",)
                )
                # Missing fetch bookkeeping alone is not missing market data.
                conn.execute("DELETE FROM jquants_daily_bars")
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            required_start = asof - timedelta(days=coverage_core.BARS_INPUT_WINDOW_DAYS)
            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars"
                    and issue.requirement == f"{required_start.isoformat()}..{asof.isoformat()}"
                    and issue.reason == "daily bars request window is not fully covered in SQLite"
                    for issue in issues
                )
            )

    def test_daily_bars_without_fetch_coverage_are_not_acquired(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM source_coverage WHERE source = ?", ("jquants_daily_bars",)
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars"
                    and issue.reason == "daily bars request window is not fully covered in SQLite"
                    for issue in issues
                )
            )

    def test_all_past_earnings_calendar_snapshot_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE jpx_earnings_calendar SET announcement_date = ?",
                    ((asof - timedelta(days=1)).isoformat(),),
                )
                conn.execute(
                    "UPDATE source_coverage SET coverage_start = ?, coverage_end = ? "
                    "WHERE source = ?",
                    (
                        (asof - timedelta(days=1)).isoformat(),
                        (asof - timedelta(days=1)).isoformat(),
                        "jpx_earnings_calendar",
                    ),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jpx_earnings_calendar"
                    and "only past known dates" in issue.reason
                    for issue in issues
                )
            )

    def test_zero_row_earnings_calendar_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute("DELETE FROM jpx_earnings_calendar")
                conn.execute(
                    "UPDATE source_coverage SET record_count = ? WHERE source = ?",
                    (0, "jpx_earnings_calendar"),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jpx_earnings_calendar" and "row count" in issue.reason
                    for issue in issues
                )
            )

    def test_source_coverage_status_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(open_connection(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE source_coverage SET status = ? WHERE source = ?",
                    ("failed", "jquants_daily_bars"),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars" and "status is not ok" in issue.reason
                    for issue in issues
                )
            )

    def test_old_non_ok_source_coverage_outside_required_window_does_not_block(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                old_start = asof - timedelta(days=2300)
                old_end = asof - timedelta(days=2270)
                conn.execute(
                    "INSERT INTO source_coverage("
                    "source, coverage_key, coverage_start, coverage_end, "
                    "fetched_at_utc, record_count, status"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "jquants_daily_bars",
                        "old-failed",
                        old_start.isoformat(),
                        old_end.isoformat(),
                        datetime.now(UTC).isoformat(),
                        0,
                        "failed",
                    ),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertFalse(
                any(
                    issue.source == "jquants_daily_bars" and "old-failed" in issue.requirement
                    for issue in issues
                ),
                issues,
            )

    def test_unrepaired_quality_failure_in_normalization_history_remains_in_scope(self) -> None:
        asof = _COMMON_COVERAGE_ASOF
        older_input = asof - timedelta(days=1400)
        for source, store, date_field, value_field in (
            ("jquants_daily_bars", store_jquants_daily_bars, "Date", "C"),
            ("jquants_fin_summaries", store_jquants_fin_summaries, "DiscDate", "Sales"),
        ):
            with self.subTest(source=source), _complete_coverage_database() as sqlite_path:
                clean = [
                    {
                        "Code": f"{1301 + index}0",
                        date_field: older_input.isoformat(),
                        value_field: 1000,
                    }
                    for index in range(100)
                ]
                if source == "jquants_fin_summaries":
                    for record in clean:
                        record["DiscNo"] = f"synthetic-{record['Code']}-{older_input.isoformat()}"
                store(
                    sqlite_path,
                    [*clean, {"Code": "invalid", date_field: older_input.isoformat()}],
                    requested_start=older_input,
                    requested_end=older_input,
                )
                issues = _verify_screening_sqlite_coverage(sqlite_path, asof)
                self.assertTrue(
                    any(
                        issue.source == source and "status is not ok: partial" in issue.reason
                        for issue in issues
                    ),
                    issues,
                )
                store(sqlite_path, clean, requested_start=older_input, requested_end=older_input)
                repaired = _verify_screening_sqlite_coverage(sqlite_path, asof)
                self.assertFalse(
                    any(
                        issue.source == source and "status is not ok" in issue.reason
                        for issue in repaired
                    ),
                    repaired,
                )

    def test_zero_row_master_import_reports_incomplete_cache(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM source_coverage WHERE source = ?", ("jquants_master_snapshots",)
                )
                conn.execute("DELETE FROM jquants_master_snapshots")
                _add_source_coverage(
                    conn,
                    source="jquants_master_snapshots",
                    coverage_key=f"get_eq_master:{asof.isoformat()}..{asof.isoformat()}",
                    record_count=0,
                    min_date=asof.isoformat(),
                    max_date=asof.isoformat(),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_master_snapshots", {issue.source for issue in issues})
            self.assertTrue(
                any("no exact master snapshot rows" in issue.reason for issue in issues)
            )

    def test_tiny_common_stock_master_reports_incomplete_cache(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE jquants_master_snapshots SET record_json=json_set(record_json,'$.ProdCat','014') WHERE substr(security_code,1,4) != ?",
                    ("1301",),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_master_snapshots"
                    and "common-stock row count" in issue.reason
                    for issue in issues
                )
            )

    def test_missing_native_product_category_is_reported_without_sector_inference(self) -> None:
        asof = _COMMON_COVERAGE_ASOF
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE jquants_master_snapshots "
                    "SET record_json=json_set(record_json,'$.ProdCat',NULL) "
                    "WHERE security_code='13010'"
                )
                issues: list[jquants_coverage.CacheCoverageIssue] = []
                jquants_coverage._append_master_snapshot_issues(conn, issues, asof_date=asof)
            self.assertTrue(
                any("1 row(s) with invalid required data" in issue.reason for issue in issues)
            )

    def test_non_common_bars_and_financials_cannot_mask_common_density(self) -> None:
        asof = _COMMON_COVERAGE_ASOF
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                insert_master_rows(
                    conn,
                    [
                        (asof.isoformat(), f"9{index:03d}", "Fund", "Prime", "その他", "014")
                        for index in range(60)
                    ],
                )
                conn.execute(
                    "DELETE FROM jquants_daily_bars WHERE traded_at = ? AND ticker >= '1341'",
                    (asof.isoformat(),),
                )
                conn.executemany(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
                    [(f"9{index:03d}", asof.isoformat(), 100.0) for index in range(60)],
                )
                conn.execute("DELETE FROM jquants_fin_summaries WHERE ticker >= '1341'")
                for index in range(60):
                    insert_financial_record(
                        conn,
                        {
                            "Code": str(f"9{index:03d}") + "0",
                            "DiscDate": asof.isoformat(),
                            "DiscNo": asof.isoformat(),
                        },
                    )
                conn.commit()
                issues: list[jquants_coverage.CacheCoverageIssue] = []
                with patch(
                    "baibai_engine.market.master_snapshot.MIN_COMMON_STOCK_MASTER_ROWS", 100
                ):
                    jquants_coverage._append_asof_bar_density_issue(conn, issues, asof_date=asof)
                    jquants_coverage._append_recent_bar_density_issue(conn, issues, asof_date=asof)
                    jquants_coverage._append_fin_summary_density_issue(
                        conn, issues, start=asof - timedelta(days=730), end=asof
                    )

            self.assertEqual(
                {issue.source for issue in issues}, {"jquants_daily_bars", "jquants_fin_summaries"}
            )
            self.assertTrue(any("recent usable ticker count" in issue.reason for issue in issues))

    def test_non_common_bars_cannot_mask_history_bucket_density(self) -> None:
        asof = _COMMON_COVERAGE_ASOF
        start = asof - timedelta(days=19)
        with tempfile.TemporaryDirectory() as tmp:
            with closing(open_connection(Path(tmp) / "market.sqlite")) as conn, conn:
                insert_master_rows(
                    conn,
                    [
                        (asof.isoformat(), f"{1301 + index:04d}", "Stock", "Prime", "機械", "011")
                        for index in range(100)
                    ]
                    + [
                        (asof.isoformat(), f"9{index:03d}", "Fund", "Prime", "その他", "014")
                        for index in range(60)
                    ],
                )
                tickers = [f"{1301 + index:04d}" for index in range(40)] + [
                    f"9{index:03d}" for index in range(60)
                ]
                conn.executemany(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
                    [
                        (ticker, (start + timedelta(days=day)).isoformat(), 100.0)
                        for day in range(5)
                        for ticker in tickers
                    ],
                )
                issues: list[jquants_coverage.CacheCoverageIssue] = []
                with patch(
                    "baibai_engine.market.master_snapshot.MIN_COMMON_STOCK_MASTER_ROWS", 100
                ):
                    jquants_coverage._append_daily_history_density_issue(
                        conn, issues, start=start, end=asof
                    )

            self.assertEqual(len(issues), 1)
            self.assertIn("(0/5 usable dates)", issues[0].reason)

    def test_master_below_production_floor_reports_incomplete_cache(self) -> None:
        asof = date(2026, 5, 8)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            with closing(open_connection(sqlite_path)) as conn, conn:
                tickers = tuple(f"{1301 + index:04d}" for index in range(2499))
                insert_master_rows(
                    conn,
                    [
                        (asof.isoformat(), ticker, f"Name {ticker}", "Prime", "水産・農林業", "011")
                        for ticker in tickers
                    ],
                )
                _add_source_coverage(
                    conn,
                    source="jquants_master_snapshots",
                    coverage_key=f"get_eq_master:{asof.isoformat()}..{asof.isoformat()}",
                    record_count=len(tickers),
                    min_date=asof.isoformat(),
                    max_date=asof.isoformat(),
                )
                conn.commit()

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
            with closing(open_connection(sqlite_path)) as conn, conn:
                tickers = tuple(f"{1301 + index:04d}" for index in range(2500))
                insert_master_rows(
                    conn,
                    [
                        (asof.isoformat(), ticker, f"Name {ticker}", "Prime", "水産・農林業", "011")
                        for ticker in tickers
                    ],
                )
                _add_source_coverage(
                    conn,
                    source="jquants_master_snapshots",
                    coverage_key=f"get_eq_master:{asof.isoformat()}..{asof.isoformat()}",
                    record_count=len(tickers),
                    min_date=asof.isoformat(),
                    max_date=asof.isoformat(),
                )
                conn.executemany(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close, turnover_value) "
                    "VALUES (?, ?, ?, ?)",
                    [(ticker, asof.isoformat(), 1000.0, 200_000_000.0) for ticker in tickers[:100]],
                )
                _add_source_coverage(
                    conn,
                    source="jquants_daily_bars",
                    coverage_key=f"get_eq_bars_daily_range:{bars_start.isoformat()}..{asof.isoformat()}",
                    record_count=100,
                    min_date=bars_start.isoformat(),
                    max_date=asof.isoformat(),
                )
                conn.commit()

            issues = verify_screening_sqlite_coverage(
                sqlite_path,
                asof,
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

    def test_required_table_without_rows_reports_incomplete_cache(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute("DELETE FROM jquants_daily_bars")
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars"
                    and (
                        "has no rows in the required date window" in issue.reason
                        or "usable date density" in issue.reason
                    )
                    for issue in issues
                )
            )

    def test_recent_daily_bars_sparse_history_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM jquants_daily_bars WHERE traded_at < ?",
                    (asof.isoformat(),),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars" and "recent 30-day window" in issue.reason
                    for issue in issues
                )
            )

    def test_old_daily_bars_sparse_history_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                weak_start = (asof - timedelta(days=1200)) + timedelta(days=240)
                weak_end = weak_start + timedelta(days=119)
                conn.execute(
                    "DELETE FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ?",
                    (weak_start.isoformat(), weak_end.isoformat()),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars" and "long-history" in issue.reason
                    for issue in issues
                )
            )

    def test_fin_summary_sparse_ticker_coverage_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM jquants_fin_summaries WHERE ticker >= ?",
                    ("1350",),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_fin_summaries"
                    and "usable ticker count" in issue.reason
                    for issue in issues
                )
            )

    def test_overlapping_daily_source_coverage_reports_count_mismatch(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                _add_source_coverage(
                    conn,
                    source="jquants_daily_bars",
                    coverage_key="overlapping-receipt",
                    record_count=1,
                    min_date=asof.isoformat(),
                    max_date=asof.isoformat(),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars" and "row count" in issue.reason
                    for issue in issues
                )
            )

    def test_market_calendar_requires_actual_asof_row(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE jquants_market_calendar SET day = ? WHERE day = ?",
                    ((asof - timedelta(days=1)).isoformat(), asof.isoformat()),
                )
                conn.commit()

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
        with _complete_coverage_database() as sqlite_path:
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
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE jpx_regulation_sources SET fetched_at_utc = ? WHERE asof_date = ?",
                    ("2026-04-01T00:00:00+00:00", asof.isoformat()),
                )
                conn.commit()

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
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE jpx_regulation_sources SET fetched_at_utc = NULL WHERE asof_date = ?",
                    (asof.isoformat(),),
                )
                conn.commit()

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
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE jpx_regulation_sources SET fetched_at_utc = ? WHERE asof_date = ?",
                    ("2026-04-01T00:00:00+00:00", asof.isoformat()),
                )
                conn.commit()

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

    def test_orphaned_jpx_source_rows_do_not_satisfy_source_coverage(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM source_coverage WHERE source = ?", ("jpx_regulation_flags",)
                )
                conn.execute(
                    "INSERT INTO jpx_regulation_sources(asof_date, source_name, fetched_at_utc) "
                    "VALUES (?, ?, ?)",
                    (asof.isoformat(), "取引停止", None),
                )
                conn.commit()

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
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "UPDATE edinet_metrics SET failure_reasons = ? WHERE asof_date = ?",
                    ("not-json", asof.isoformat()),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "edinet_metrics" and "eligibility query failed" in issue.reason
                    for issue in issues
                )
            )

    def test_failed_edinet_extraction_does_not_block_optional_inputs(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            with closing(sqlite3.connect(sqlite_path)) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO source_coverage("
                    "source, coverage_key, coverage_start, coverage_end, "
                    "fetched_at_utc, record_count, "
                    "status, error"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "edinet_metrics",
                        asof.isoformat(),
                        asof.isoformat(),
                        asof.isoformat(),
                        datetime.now(UTC).isoformat(),
                        1,
                        "failed",
                        "1 EDINET CSV hard failures",
                    ),
                )
                conn.commit()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertFalse(any(issue.source == "edinet_metrics" for issue in issues))
            with closing(sqlite3.connect(sqlite_path)) as conn:
                status, error = conn.execute(
                    "SELECT status, error FROM source_coverage "
                    "WHERE source = 'edinet_metrics' AND coverage_key = ?",
                    (asof.isoformat(),),
                ).fetchone()
            self.assertEqual((status, error), ("failed", "1 EDINET CSV hard failures"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


def test_fetched_empty_financial_history_is_not_sufficient_for_screening() -> None:
    from baibai_engine.storage.observations.coverage import range_covered

    asof = _COMMON_COVERAGE_ASOF
    with _complete_coverage_database() as database:
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DELETE FROM jquants_fin_summaries")
            connection.execute(
                "UPDATE source_coverage SET record_count = 0 WHERE source = 'jquants_fin_summaries'"
            )
            assert range_covered(
                connection, "jquants_fin_summaries", asof - timedelta(days=2200), asof
            )
        issues = _verify_screening_sqlite_coverage(database, asof)
        assert any(
            issue.source == "jquants_fin_summaries"
            and "has no rows in the required date window" in issue.reason
            for issue in issues
        )
