from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tests.helpers.screening_sqlite import add_source_coverage as _add_source_coverage

from baibai_engine.screening.sqlite_cache import open_connection
from baibai_engine.screening.sqlite_coverage import core as coverage_core
from baibai_engine.screening.sqlite_coverage import (
    plan_required_field_repair,
    read_required_field_coverage,
    verify_screening_sqlite_coverage,
)

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
    with patch("baibai_engine.screening.master_snapshot.MIN_COMMON_STOCK_MASTER_ROWS", 100):
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
    conn.executemany(
        "INSERT OR REPLACE INTO jquants_master_snapshots("
        "snapshot_date, ticker, name, market, sector_33, is_common_stock"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        [
            (asof.isoformat(), ticker, f"Name {ticker}", "Prime", "水産・農林業", 1)
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
        coverage_key=(
            "jquants:"
            f"get_eq_bars_daily_range-end_dt-{asof.isoformat()}-"
            f"start_dt-{bars_start.isoformat()}.json"
        ),
        record_count=len(bar_rows),
        min_date=bars_start.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO jquants_fin_summaries("
        "ticker, disclosed_at, eps_ttm, shares_outstanding, treasury_shares, "
        "equity_to_asset_ratio) VALUES (?, ?, ?, ?, ?, ?)",
        [(ticker, asof.isoformat(), 100.0, 10_000_000.0, 1_000_000.0, 0.5) for ticker in tickers],
    )
    _add_source_coverage(
        conn,
        source="jquants_fin_summaries",
        coverage_key=(
            "jquants:"
            f"get_fin_summary_range-end_dt-{asof.isoformat()}-"
            f"start_dt-{fin_start.isoformat()}.json"
        ),
        record_count=len(tickers),
        min_date=fin_start.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.execute(
        "INSERT OR REPLACE INTO jquants_earnings_calendar(announcement_date, ticker) VALUES (?, ?)",
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
        "INSERT OR REPLACE INTO jquants_market_calendar(day, is_business_day) VALUES (?, ?)",
        (asof.isoformat(), 1),
    )
    _add_source_coverage(
        conn,
        source="jquants_market_calendar",
        coverage_key=(
            f"jquants:get_mkt_calendar-from_yyyymmdd-{asof:%Y%m%d}-to_yyyymmdd-{asof:%Y%m%d}.json"
        ),
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
        coverage_key=f"edinet_metrics:{asof.isoformat()}",
        record_count=1,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.execute(
        "INSERT OR REPLACE INTO edinet_metrics(asof_date, ticker, sales_ttm) VALUES (?, ?, ?)",
        (asof.isoformat(), "1301", 1_000_000.0),
    )


def _populate_complete_coverage(conn: sqlite3.Connection, asof: date) -> None:
    _seed_complete_coverage(conn, asof)


def _complete_coverage_template_path() -> Path:
    global _COMPLETE_COVERAGE_TEMPLATE_DIR, _COMPLETE_COVERAGE_TEMPLATE_PATH
    if _COMPLETE_COVERAGE_TEMPLATE_PATH is None:
        template_dir = tempfile.TemporaryDirectory()
        template_path = Path(template_dir.name) / "market.sqlite"
        conn = open_connection(template_path)
        _seed_complete_coverage(conn, _COMMON_COVERAGE_ASOF)
        conn.commit()
        conn.close()
        _COMPLETE_COVERAGE_TEMPLATE_DIR = template_dir
        _COMPLETE_COVERAGE_TEMPLATE_PATH = template_path
    return _COMPLETE_COVERAGE_TEMPLATE_PATH


@contextmanager
def _complete_coverage_database() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        sqlite_path = Path(tmp) / "market.sqlite"
        shutil.copyfile(_complete_coverage_template_path(), sqlite_path)
        yield sqlite_path


def tearDownModule() -> None:
    global _COMPLETE_COVERAGE_TEMPLATE_DIR, _COMPLETE_COVERAGE_TEMPLATE_PATH
    if _COMPLETE_COVERAGE_TEMPLATE_DIR is not None:
        _COMPLETE_COVERAGE_TEMPLATE_DIR.cleanup()
        _COMPLETE_COVERAGE_TEMPLATE_DIR = None
        _COMPLETE_COVERAGE_TEMPLATE_PATH = None


class SQLiteCoverageTests(unittest.TestCase):
    def test_complete_coverage_clones_are_isolated(self) -> None:
        with _complete_coverage_database() as first_path:
            first = sqlite3.connect(first_path)
            first.execute("DELETE FROM jquants_master_snapshots")
            first.commit()
            first_count = int(
                first.execute("SELECT COUNT(*) FROM jquants_master_snapshots").fetchone()[0]
            )
            first.close()

        with _complete_coverage_database() as second_path:
            second = sqlite3.connect(second_path)
            second_count = int(
                second.execute("SELECT COUNT(*) FROM jquants_master_snapshots").fetchone()[0]
            )
            second.close()

            self.assertEqual(first_count, 0)
            self.assertEqual(second_count, 100)

    def test_two_master_dates_verify_independently_through_public_coverage_gate(self) -> None:
        first = date(2026, 5, 8)
        second = date(2026, 5, 15)
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            _populate_complete_coverage(conn, first)
            _populate_complete_coverage(conn, second)

            bars_start = first - timedelta(days=2200)
            fin_start = first - timedelta(days=2200)
            conn.execute("DELETE FROM source_coverage WHERE source = ?", ("jquants_daily_bars",))
            bars_count = int(conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0])
            _add_source_coverage(
                conn,
                source="jquants_daily_bars",
                coverage_key=f"get_eq_bars_daily_range:{bars_start}..{second}",
                record_count=bars_count,
                min_date=bars_start.isoformat(),
                max_date=second.isoformat(),
            )
            conn.execute("DELETE FROM source_coverage WHERE source = ?", ("jquants_fin_summaries",))
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
                "FROM jquants_earnings_calendar"
            ).fetchone()
            conn.execute(
                "UPDATE source_coverage SET coverage_start = ?, coverage_end = ?, "
                "record_count = ? WHERE source = ?",
                (*earnings_range, "jpx_earnings_calendar"),
            )
            conn.commit()
            conn.close()

            self.assertEqual(_verify_screening_sqlite_coverage(sqlite_path, first), ())
            self.assertEqual(_verify_screening_sqlite_coverage(sqlite_path, second), ())

            conn = open_connection(sqlite_path)
            conn.execute(
                "DELETE FROM jquants_master_snapshots WHERE snapshot_date = ? AND ticker = ?",
                (first.isoformat(), "1301"),
            )
            conn.commit()
            conn.close()

            first_issues = _verify_screening_sqlite_coverage(sqlite_path, first)
            second_issues = _verify_screening_sqlite_coverage(sqlite_path, second)
            self.assertTrue(
                any(issue.source == "jquants_master_snapshots" for issue in first_issues)
            )
            self.assertEqual(second_issues, ())

            conn = open_connection(sqlite_path)
            conn.execute(
                "INSERT INTO jquants_master_snapshots("
                "snapshot_date, ticker, name, market, sector_33, is_common_stock"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (first.isoformat(), "1301", "name-1301", "Prime", "Sector", 1),
            )
            conn.execute(
                "DELETE FROM source_coverage WHERE source = ? AND coverage_key = ?",
                (
                    "jquants_master_snapshots",
                    f"get_eq_master:{second.isoformat()}..{second.isoformat()}",
                ),
            )
            conn.commit()
            conn.close()

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
        """The deep check costs ~11s on the production store and runs twice a batch.

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
            conn = sqlite3.connect(sqlite_path)
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertEqual(issues, ())

    def test_issue_866_null_fields_block_before_market_cap_candidate_wipeout(self) -> None:
        """A migrated column is not usable merely because the old rows still cover the date."""
        asof = _COMMON_COVERAGE_ASOF
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE jquants_fin_summaries SET shares_outstanding = NULL, "
                "treasury_shares = NULL, equity_to_asset_ratio = NULL"
            )
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)
            coverage = read_required_field_coverage(
                sqlite_path,
                start=asof - timedelta(days=730),
                asof=asof,
            )

            self.assertIsNotNone(coverage)
            assert coverage is not None
            self.assertEqual(coverage.summary_tickers, 100)
            self.assertEqual(coverage.market_cap_required_fields_tickers, 0)
            self.assertIn("market_cap_required_fields=0", coverage.summary_line())
            self.assertIn("valuation_required_fields=0", coverage.summary_line())
            requirements = {issue.requirement for issue in issues}
            self.assertIn(f"required-field:shares_outstanding@{asof.isoformat()}", requirements)
            self.assertIn(f"required-field:treasury_shares@{asof.isoformat()}", requirements)
            self.assertIn(f"required-field:equity_to_asset_ratio@{asof.isoformat()}", requirements)
            self.assertIn(
                f"required-field:market_cap_required_fields@{asof.isoformat()}", requirements
            )
            self.assertIn(
                f"required-field:valuation_required_fields@{asof.isoformat()}", requirements
            )

    def test_required_field_population_threshold_blocks_74_percent_and_accepts_75(self) -> None:
        asof = _COMMON_COVERAGE_ASOF
        start = asof - timedelta(days=730)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE jquants_fin_summaries SET shares_outstanding = NULL, "
                "treasury_shares = NULL, equity_to_asset_ratio = NULL"
            )
            conn.execute(
                "UPDATE jquants_fin_summaries SET shares_outstanding = 10000000, "
                "treasury_shares = 1000000, equity_to_asset_ratio = 0.5 "
                "WHERE ticker IN (SELECT ticker FROM jquants_fin_summaries ORDER BY ticker LIMIT 74)"
            )
            conn.commit()
            conn.close()

            below = read_required_field_coverage(sqlite_path, start=start, asof=asof)
            self.assertIsNotNone(below)
            assert below is not None
            self.assertEqual(below.minimum_tickers, 75)
            self.assertIn("market_cap_required_fields", below.blocking_fields)
            self.assertIn("valuation_required_fields", below.blocking_fields)

            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE jquants_fin_summaries SET shares_outstanding = 10000000, "
                "treasury_shares = 1000000, equity_to_asset_ratio = 0.5 "
                "WHERE ticker = (SELECT ticker FROM jquants_fin_summaries "
                "ORDER BY ticker LIMIT 1 OFFSET 74)"
            )
            conn.commit()
            conn.close()

            at_threshold = read_required_field_coverage(sqlite_path, start=start, asof=asof)
            self.assertIsNotNone(at_threshold)
            assert at_threshold is not None
            self.assertEqual(at_threshold.blocking_fields, ())

    def test_valuation_field_intersection_is_checked_not_only_each_column(self) -> None:
        asof = _COMMON_COVERAGE_ASOF
        start = asof - timedelta(days=730)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE jquants_fin_summaries SET shares_outstanding = NULL, "
                "treasury_shares = NULL, equity_to_asset_ratio = NULL"
            )
            conn.execute(
                "UPDATE jquants_fin_summaries SET shares_outstanding = 10000000, "
                "treasury_shares = 1000000 WHERE ticker IN "
                "(SELECT ticker FROM jquants_fin_summaries ORDER BY ticker LIMIT 80)"
            )
            conn.execute(
                "UPDATE jquants_fin_summaries SET equity_to_asset_ratio = 0.5 WHERE ticker IN "
                "(SELECT ticker FROM jquants_fin_summaries ORDER BY ticker LIMIT 80 OFFSET 20)"
            )
            conn.commit()
            conn.close()

            coverage = read_required_field_coverage(sqlite_path, start=start, asof=asof)

            self.assertIsNotNone(coverage)
            assert coverage is not None
            self.assertEqual(coverage.shares_outstanding_tickers, 80)
            self.assertEqual(coverage.treasury_shares_tickers, 80)
            self.assertEqual(coverage.equity_to_asset_ratio_tickers, 80)
            self.assertEqual(coverage.market_cap_required_fields_tickers, 80)
            self.assertEqual(coverage.valuation_required_fields_tickers, 60)
            self.assertEqual(coverage.blocking_fields, ("valuation_required_fields",))

    def test_non_null_share_fields_that_produce_no_market_cap_are_blocked(self) -> None:
        asof = _COMMON_COVERAGE_ASOF
        start = asof - timedelta(days=730)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE jquants_fin_summaries SET shares_outstanding = 1000000, "
                "treasury_shares = 1000000"
            )
            conn.commit()
            conn.close()

            coverage = read_required_field_coverage(sqlite_path, start=start, asof=asof)
            plan = plan_required_field_repair(sqlite_path, start=start, asof=asof)

            self.assertIsNotNone(coverage)
            assert coverage is not None
            self.assertEqual(coverage.shares_outstanding_tickers, 100)
            self.assertEqual(coverage.treasury_shares_tickers, 100)
            self.assertEqual(coverage.market_cap_required_fields_tickers, 0)
            self.assertEqual(coverage.valuation_required_fields_tickers, 0)
            self.assertEqual(
                coverage.blocking_fields,
                ("market_cap_required_fields", "valuation_required_fields"),
            )
            self.assertIsNotNone(plan)
            assert plan is not None
            self.assertEqual(plan.ranges, ((asof, asof),))

    def test_cross_date_share_fields_use_the_same_split_basis_as_market_cap(self) -> None:
        asof = _COMMON_COVERAGE_ASOF
        start = asof - timedelta(days=730)
        prior = asof - timedelta(days=30)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            tickers = [
                str(row[0])
                for row in conn.execute(
                    "SELECT ticker FROM jquants_fin_summaries ORDER BY ticker"
                ).fetchall()
            ]
            conn.execute(
                "UPDATE jquants_fin_summaries SET shares_outstanding = NULL, "
                "treasury_shares = 1500000"
            )
            conn.executemany(
                "INSERT INTO jquants_fin_summaries("
                "ticker, disclosed_at, shares_outstanding, treasury_shares) "
                "VALUES (?, ?, ?, ?)",
                [(ticker, prior.isoformat(), 1_000_000.0, None) for ticker in tickers],
            )
            conn.commit()
            conn.close()

            coverage = read_required_field_coverage(sqlite_path, start=start, asof=asof)

            self.assertIsNotNone(coverage)
            assert coverage is not None
            self.assertEqual(coverage.shares_outstanding_tickers, 100)
            self.assertEqual(coverage.treasury_shares_tickers, 100)
            self.assertEqual(coverage.market_cap_required_fields_tickers, 0)
            self.assertIn("market_cap_required_fields", coverage.blocking_fields)

            split_day = prior + timedelta(days=1)
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE jquants_daily_bars SET adjustment_factor = 0.5 WHERE traded_at = ?",
                (split_day.isoformat(),),
            )
            conn.commit()
            conn.close()

            split_adjusted = read_required_field_coverage(sqlite_path, start=start, asof=asof)
            self.assertIsNotNone(split_adjusted)
            assert split_adjusted is not None
            self.assertEqual(split_adjusted.market_cap_required_fields_tickers, 100)
            self.assertEqual(split_adjusted.blocking_fields, ())

    def test_old_cross_section_repair_resumes_at_only_the_unfinished_disclosure_date(
        self,
    ) -> None:
        """Completed repair dates are inferred from rows, without erasing broad coverage."""
        asof = _COMMON_COVERAGE_ASOF
        start = asof - timedelta(days=730)
        first_date = asof - timedelta(days=400)
        second_date = asof - timedelta(days=200)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE jquants_fin_summaries SET disclosed_at = ?, shares_outstanding = NULL, "
                "treasury_shares = NULL, equity_to_asset_ratio = NULL "
                "WHERE ticker >= '1321' AND ticker <= '1360'",
                (first_date.isoformat(),),
            )
            conn.execute(
                "UPDATE jquants_fin_summaries SET disclosed_at = ?, shares_outstanding = NULL, "
                "treasury_shares = NULL, equity_to_asset_ratio = NULL "
                "WHERE ticker >= '1361'",
                (second_date.isoformat(),),
            )
            original_coverage = conn.execute(
                "SELECT coverage_key, coverage_start, coverage_end FROM source_coverage "
                "WHERE source = 'jquants_fin_summaries'"
            ).fetchall()
            conn.commit()
            conn.close()

            initial = plan_required_field_repair(sqlite_path, start=start, asof=asof)
            self.assertIsNotNone(initial)
            assert initial is not None
            self.assertEqual(
                initial.ranges,
                ((first_date, first_date), (second_date, second_date)),
            )

            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE jquants_fin_summaries SET shares_outstanding = 10000000, "
                "treasury_shares = 1000000, equity_to_asset_ratio = 0.5 "
                "WHERE disclosed_at = ?",
                (first_date.isoformat(),),
            )
            conn.commit()
            conn.close()

            resumed = plan_required_field_repair(sqlite_path, start=start, asof=asof)
            self.assertIsNotNone(resumed)
            assert resumed is not None
            self.assertEqual(resumed.ranges, ((second_date, second_date),))

            conn = sqlite3.connect(sqlite_path)
            preserved_coverage = conn.execute(
                "SELECT coverage_key, coverage_start, coverage_end FROM source_coverage "
                "WHERE source = 'jquants_fin_summaries'"
            ).fetchall()
            conn.close()
            self.assertEqual(preserved_coverage, original_coverage)

    def test_current_bar_window_cannot_satisfy_normalized_split_basis(self) -> None:
        asof = date(2026, 5, 8)
        current_start = asof - timedelta(days=1200)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "DELETE FROM jquants_daily_bars WHERE traded_at < ?",
                (current_start.isoformat(),),
            )
            conn.commit()
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "DELETE FROM jquants_daily_bars WHERE traded_at < ? AND ticker != '1301'",
                (current_start.isoformat(),),
            )
            actual_count = int(
                conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0]
            )
            conn.execute(
                "UPDATE source_coverage SET record_count = ? WHERE source = 'jquants_daily_bars'",
                (actual_count,),
            )
            conn.commit()
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE source_coverage SET coverage_start = ? "
                "WHERE source = 'jquants_fin_summaries'",
                (current_start.isoformat(),),
            )
            conn.commit()
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
            conn.execute("DROP TABLE edinet_documents")
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].source, "sqlite")
            self.assertIn("missing tables: edinet_documents", issues[0].reason)

    def test_missing_required_column_reports_schema_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute("ALTER TABLE edinet_documents DROP COLUMN doc_description")
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].source, "sqlite")
            self.assertIn("missing columns in edinet_documents: doc_description", issues[0].reason)

    def test_required_table_without_primary_key_reports_schema_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute("DROP TABLE jquants_daily_bars")
            conn.execute(
                """
                CREATE TABLE jquants_daily_bars(
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
                  lower_limit TEXT
                )
                """
            )
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].source, "sqlite")
            self.assertIn("table shape mismatch: jquants_daily_bars", issues[0].reason)

    def test_missing_required_index_reports_schema_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute("DROP INDEX idx_jquants_daily_bars_traded_at")
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].source, "sqlite")
            self.assertIn(
                "index shape mismatch: idx_jquants_daily_bars_traded_at", issues[0].reason
            )

    def test_daily_bar_table_count_below_source_coverage_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute("DELETE FROM jquants_daily_bars WHERE ticker >= ?", ("1351",))
            conn.commit()
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE source_coverage SET record_count = ? WHERE source = ?",
                (1, "jquants_daily_bars"),
            )
            conn.commit()
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "INSERT INTO jquants_daily_bars(ticker, traded_at, close, turnover_value) "
                "VALUES (?, ?, ?, ?)",
                ("1301", (asof + timedelta(days=1)).isoformat(), 1000.0, 200_000_000.0),
            )
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertFalse(
                any(
                    issue.source == "jquants_daily_bars"
                    and "row count" in issue.reason
                    and "source_coverage record_count" in issue.reason
                    for issue in issues
                )
            )

    def test_daily_bar_required_window_count_mismatch_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
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
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
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
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
            conn.execute("DELETE FROM jquants_fin_summaries WHERE ticker >= ?", ("1351",))
            conn.commit()
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE source_coverage SET record_count = ? WHERE source = ?",
                (1, "jquants_fin_summaries"),
            )
            conn.commit()
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "DELETE FROM jquants_fin_summaries WHERE ticker = ? AND disclosed_at = ?",
                ("1301", asof.isoformat()),
            )
            conn.execute(
                "INSERT INTO jquants_fin_summaries(ticker, disclosed_at, eps_ttm) VALUES (?, ?, ?)",
                ("9999", (asof + timedelta(days=1)).isoformat(), 100.0),
            )
            conn.commit()
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
            conn.execute("DELETE FROM source_coverage WHERE source = ?", ("jquants_daily_bars",))
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_daily_bars", {issue.source for issue in issues})

    def test_all_past_earnings_calendar_snapshot_reports_issue(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE jquants_earnings_calendar SET announcement_date = ?",
                ((asof - timedelta(days=1)).isoformat(),),
            )
            conn.execute(
                "UPDATE source_coverage SET coverage_start = ?, coverage_end = ? WHERE source = ?",
                (
                    (asof - timedelta(days=1)).isoformat(),
                    (asof - timedelta(days=1)).isoformat(),
                    "jpx_earnings_calendar",
                ),
            )
            conn.commit()
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
            conn.execute("DELETE FROM jquants_earnings_calendar")
            conn.execute(
                "UPDATE source_coverage SET record_count = ? WHERE source = ?",
                (0, "jpx_earnings_calendar"),
            )
            conn.commit()
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
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
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            old_start = asof - timedelta(days=1300)
            old_end = asof - timedelta(days=1270)
            conn.execute(
                "INSERT INTO source_coverage("
                "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, record_count, status"
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
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertFalse(
                any(
                    issue.source == "jquants_daily_bars" and "old-failed" in issue.requirement
                    for issue in issues
                ),
                issues,
            )

    def test_zero_row_master_import_reports_incomplete_cache(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
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
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertIn("jquants_master_snapshots", {issue.source for issue in issues})
            self.assertTrue(
                any("no exact master snapshot rows" in issue.reason for issue in issues)
            )

    def test_tiny_common_stock_master_reports_incomplete_cache(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE jquants_master_snapshots SET is_common_stock = 0 WHERE ticker != ?",
                ("1301",),
            )
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
                "snapshot_date, ticker, name, market, sector_33, is_common_stock"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        asof.isoformat(),
                        ticker,
                        f"Name {ticker}",
                        "Prime",
                        "水産・農林業",
                        1,
                    )
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
                "snapshot_date, ticker, name, market, sector_33, is_common_stock"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        asof.isoformat(),
                        ticker,
                        f"Name {ticker}",
                        "Prime",
                        "水産・農林業",
                        1,
                    )
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
                coverage_key=(
                    "jquants:"
                    f"get_eq_bars_daily_range-end_dt-{asof.isoformat()}-"
                    f"start_dt-{bars_start.isoformat()}.json"
                ),
                record_count=100,
                min_date=bars_start.isoformat(),
                max_date=asof.isoformat(),
            )
            conn.commit()
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
            conn.execute("DELETE FROM jquants_daily_bars")
            conn.commit()
            conn.close()

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
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "DELETE FROM jquants_daily_bars WHERE traded_at < ?",
                (asof.isoformat(),),
            )
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
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            weak_start = (asof - timedelta(days=1200)) + timedelta(days=240)
            weak_end = weak_start + timedelta(days=119)
            conn.execute(
                "DELETE FROM jquants_daily_bars WHERE traded_at BETWEEN ? AND ?",
                (weak_start.isoformat(), weak_end.isoformat()),
            )
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
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "DELETE FROM jquants_fin_summaries WHERE ticker >= ?",
                ("1350",),
            )
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

    def test_overlapping_daily_source_coverage_reports_count_mismatch(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            _add_source_coverage(
                conn,
                source="jquants_daily_bars",
                coverage_key=(
                    "jquants:"
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

            self.assertTrue(
                any(
                    issue.source == "jquants_daily_bars" and "row count" in issue.reason
                    for issue in issues
                )
            )

    def test_market_calendar_requires_actual_asof_row(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
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
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
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
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
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
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
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
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
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

    def test_orphaned_jpx_source_rows_do_not_satisfy_source_coverage(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute("DELETE FROM source_coverage WHERE source = ?", ("jpx_regulation_flags",))
            conn.execute(
                "INSERT INTO jpx_regulation_sources(asof_date, source_name, fetched_at_utc) "
                "VALUES (?, ?, ?)",
                (asof.isoformat(), "取引停止", None),
            )
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
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "UPDATE edinet_metrics SET failure_reasons = ? WHERE asof_date = ?",
                ("not-json", asof.isoformat()),
            )
            conn.commit()
            conn.close()

            issues = _verify_screening_sqlite_coverage(sqlite_path, asof)

            self.assertTrue(
                any(
                    issue.source == "edinet_metrics" and "coverage query failed" in issue.reason
                    for issue in issues
                )
            )

    def test_required_edinet_failed_status_reports_diagnostic_error(self) -> None:
        asof = date(2026, 5, 8)
        with _complete_coverage_database() as sqlite_path:
            conn = sqlite3.connect(sqlite_path)
            conn.execute(
                "INSERT OR REPLACE INTO source_coverage("
                "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, record_count, "
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
