from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.market.store import read_daily_bars
from baibai_engine.screening.providers.jpx import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
)
from baibai_engine.screening.sqlite_cache import (
    open_connection,
    store_edinet_metrics,
    store_jpx_earnings_calendar_snapshot,
    store_jquants_daily_bars,
)
from baibai_engine.screening.sqlite_reader import (
    EDINETMetricBaselineError,
    fin_summaries_readable_from,
    read_edinet_metric_baseline,
    read_eq_master_asof,
    read_fin_summaries,
    read_jpx_earnings_calendar_snapshot,
)


def _add_source_coverage(
    conn: sqlite3.Connection,
    *,
    source: str,
    path: str,
    record_count: int,
    min_date: str | None,
    max_date: str | None,
) -> None:
    fetched_at = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO source_coverage("
        "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, record_count, status"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, path, min_date, max_date, fetched_at, record_count, "ok"),
    )


class ReadEqMasterAsOfTests(unittest.TestCase):
    def test_asof_reader_prefers_prior_and_falls_back_to_earliest_future_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            conn.executemany(
                "INSERT INTO jquants_master_snapshots("
                "snapshot_date, ticker, name, market, sector_33, is_common_stock"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("2025-01-31", "1301", "Historical", "プライム", "水産", 1),
                    ("2025-02-28", "1302", "Future", "プライム", "水産", 1),
                ],
            )
            conn.commit()
            conn.close()

            prior = read_eq_master_asof(db, date(2025, 2, 15))
            self.assertEqual(prior.status, "prior_snapshot")
            self.assertEqual(prior.snapshot_date, date(2025, 1, 31))
            self.assertEqual([master.code for master in prior.masters], ["1301"])

            fallback = read_eq_master_asof(db, date(2025, 1, 1))
            self.assertEqual(fallback.status, "future_snapshot")
            self.assertEqual(fallback.snapshot_date, date(2025, 1, 31))
            self.assertEqual([master.code for master in fallback.masters], ["1301"])

    def test_asof_reader_is_unavailable_without_any_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            conn.commit()
            conn.close()

            unavailable = read_eq_master_asof(db, date(2025, 1, 1))
            self.assertEqual(unavailable.status, "unavailable")
            self.assertEqual(unavailable.masters, ())


class ReadDailyBarsTests(unittest.TestCase):
    def test_returns_none_when_sqlite_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                read_daily_bars(Path(tmp) / "missing.sqlite", date(2024, 3, 19), date(2024, 4, 18))
            )

    def test_returns_none_when_schema_column_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            conn.execute("ALTER TABLE jquants_daily_bars DROP COLUMN upper_limit")
            conn.commit()
            conn.close()

            self.assertIsNone(read_daily_bars(db, date(2024, 3, 19), date(2024, 4, 18)))

    def test_returns_none_when_range_not_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            _add_source_coverage(
                conn,
                source="jquants_daily_bars",
                path="chunk1.json",
                record_count=1,
                min_date="2024-04-01",
                max_date="2024-04-10",
            )
            conn.commit()
            conn.close()

            # Requested range starts before imported window.
            self.assertIsNone(read_daily_bars(db, date(2024, 3, 19), date(2024, 4, 5)))

    def test_returns_bars_in_range_when_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            conn.executemany(
                "INSERT INTO jquants_daily_bars("
                "ticker, traded_at, close, turnover_value, adjustment_close, adjustment_factor"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("1301", "2024-03-19", 3790.0, 1000.0, 3790.0, 1.0),
                    ("1301", "2024-03-21", 3800.0, 1200.0, 3800.0, 0.5),
                    ("1301", "2024-04-19", 3900.0, 1500.0, 3900.0, 1.0),
                ],
            )
            _add_source_coverage(
                conn,
                source="jquants_daily_bars",
                path="chunk.json",
                record_count=3,
                min_date="2024-03-19",
                max_date="2024-04-19",
            )
            conn.commit()
            conn.close()

            bars = read_daily_bars(db, date(2024, 3, 19), date(2024, 3, 31))
            assert bars is not None
            self.assertEqual(len(bars), 2)
            self.assertEqual(bars[0].traded_at, date(2024, 3, 19))
            self.assertEqual(bars[0].close, 3790.0)
            self.assertEqual(bars[1].adjustment_factor, 0.5)

    def test_returns_none_when_only_zero_row_chunk_covers_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            _add_source_coverage(
                conn,
                source="jquants_daily_bars",
                path=".cache/screening/raw/jquants/"
                "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-19.json",
                record_count=0,
                min_date=None,
                max_date=None,
            )
            conn.commit()
            conn.close()

            self.assertIsNone(read_daily_bars(db, date(2024, 3, 19), date(2024, 4, 18)))

    def test_returns_none_when_source_coverage_status_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_daily_bars(
                db,
                [{"Code": "13010", "Date": "2024-03-19", "Close": 3790.0}],
                requested_start=date(2024, 3, 19),
                requested_end=date(2024, 4, 18),
            )
            conn = open_connection(db)
            conn.execute(
                "UPDATE source_coverage SET status = ? WHERE source = ?",
                ("failed", "jquants_daily_bars"),
            )
            conn.commit()
            conn.close()

            self.assertIsNone(read_daily_bars(db, date(2024, 3, 19), date(2024, 4, 18)))


class ReadFinSummariesTests(unittest.TestCase):
    def test_returns_summaries_in_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            conn.execute(
                "INSERT INTO jquants_fin_summaries("
                "ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding, "
                "sales, operating_profit, ordinary_profit, profit, "
                "fiscal_period, fiscal_year_end, period_start, period_end"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "3447",
                    "2025-09-29",
                    None,
                    50.0,
                    None,
                    1_000_000.0,
                    5_000_000.0,
                    1_000_000.0,
                    950_000.0,
                    700_000.0,
                    "2Q",
                    "2026-03-31",
                    "2025-04-01",
                    "2025-09-30",
                ),
            )
            _add_source_coverage(
                conn,
                source="jquants_fin_summaries",
                path=".cache/screening/raw/jquants/"
                "get_fin_summary_range-end_dt-2025-10-28-start_dt-2025-09-28.json",
                record_count=1,
                min_date="2025-09-28",
                max_date="2025-10-28",
            )
            conn.commit()
            conn.close()

            summaries = read_fin_summaries(db, date(2025, 9, 28), date(2025, 10, 28))
            assert summaries is not None
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].ticker, "3447")
            self.assertEqual(summaries[0].eps_ttm, 50.0)
            self.assertEqual(summaries[0].fiscal_period, "2Q")

    def test_returns_none_when_zero_row_chunk_covers_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            _add_source_coverage(
                conn,
                source="jquants_fin_summaries",
                path=".cache/screening/raw/jquants/"
                "get_fin_summary_range-end_dt-2025-10-28-start_dt-2025-09-28.json",
                record_count=0,
                min_date=None,
                max_date=None,
            )
            conn.commit()
            conn.close()

            self.assertIsNone(read_fin_summaries(db, date(2025, 9, 28), date(2025, 10, 28)))


class FinSummariesReadableFromTests(unittest.TestCase):
    """The floor a caller may read from, which is not where the oldest row sits."""

    @staticmethod
    def _store_with_windows(tmp: str, windows: tuple[tuple[str, str, str, int], ...]) -> Path:
        db = Path(tmp) / "market.sqlite"
        conn = open_connection(db)
        for coverage_key, min_date, max_date, record_count in windows:
            _add_source_coverage(
                conn,
                source="jquants_fin_summaries",
                path=coverage_key,
                record_count=record_count,
                min_date=min_date,
                max_date=max_date,
            )
        conn.commit()
        conn.close()
        return db

    def test_returns_the_start_of_the_window_holding_the_asof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._store_with_windows(tmp, (("a", "2016-08-11", "2026-08-11", 180_186),))
            self.assertEqual(fin_summaries_readable_from(db, date(2025, 6, 30)), date(2016, 8, 11))

    def test_abutting_windows_read_as_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._store_with_windows(
                tmp,
                (
                    ("head", "2016-08-01", "2016-08-10", 1_911),
                    ("body", "2016-08-11", "2026-08-11", 180_186),
                ),
            )
            self.assertEqual(fin_summaries_readable_from(db, date(2025, 6, 30)), date(2016, 8, 1))

    def test_a_gap_before_the_asof_answers_nothing(self) -> None:
        """A hole in the middle is an outage, not a store that starts later.

        Answering with the later window's start would hand back a quietly shorter
        history that reads exactly like a young store, so the months nobody fetched
        would never surface.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = self._store_with_windows(
                tmp,
                (
                    ("old", "2016-08-01", "2018-12-31", 100),
                    ("recent", "2020-01-01", "2026-08-11", 100),
                ),
            )
            self.assertIsNone(fin_summaries_readable_from(db, date(2025, 6, 30)))

    def test_the_only_window_answers_even_when_it_starts_late(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._store_with_windows(tmp, (("recent", "2020-01-01", "2026-08-11", 100),))
            self.assertEqual(fin_summaries_readable_from(db, date(2025, 6, 30)), date(2020, 1, 1))

    def test_returns_none_when_no_window_holds_the_asof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._store_with_windows(tmp, (("a", "2016-08-11", "2020-12-31", 100),))
            self.assertIsNone(fin_summaries_readable_from(db, date(2025, 6, 30)))

    def test_a_window_recorded_with_no_rows_is_not_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = self._store_with_windows(tmp, (("a", "2016-08-11", "2026-08-11", 0),))
            self.assertIsNone(fin_summaries_readable_from(db, date(2025, 6, 30)))

    def test_returns_none_when_sqlite_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                fin_summaries_readable_from(Path(tmp) / "missing.sqlite", date(2025, 6, 30))
            )


class ReadJPXEarningsCalendarTests(unittest.TestCase):
    def test_returns_none_when_earnings_calendar_not_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            conn.execute(
                "INSERT INTO jpx_earnings_calendar(announcement_date, ticker) VALUES (?, ?)",
                ("2026-05-15", "1301"),
            )
            conn.commit()
            conn.close()

            self.assertIsNone(read_jpx_earnings_calendar_snapshot(db, date(2026, 5, 8)))

    def test_returns_records_from_covered_sparse_whole_list_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jpx_earnings_calendar_snapshot(
                db,
                JPXEarningsCalendarSnapshot(
                    entries=(
                        JPXEarningsCalendarEntry(
                            ticker="1301", announcement_date=date(2026, 5, 15)
                        ),
                    ),
                    source_urls=("https://www.jpx.co.jp/kessan.xlsx",),
                    raw_record_count=2,
                    excluded_record_count=1,
                    superseded_record_count=None,
                ),
                fetched_at_utc="2026-05-08T00:00:00+00:00",
            )

            snapshot = read_jpx_earnings_calendar_snapshot(db, date(2026, 5, 8))

            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.entries[0].ticker, "1301")
            self.assertEqual(snapshot.entries[0].announcement_date, date(2026, 5, 15))

    def test_returns_none_when_covered_whole_list_has_zero_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            conn.execute(
                "INSERT INTO source_coverage(source, coverage_key, coverage_start, "
                "coverage_end, fetched_at_utc, record_count, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "jpx_earnings_calendar",
                    "get_earnings_calendar_snapshot:current",
                    "2026-05-08",
                    "2026-08-06",
                    datetime.now(UTC).isoformat(),
                    0,
                    "ok",
                ),
            )
            conn.commit()
            conn.close()

            self.assertIsNone(read_jpx_earnings_calendar_snapshot(db, date(2026, 5, 8)))


class ReadEDINETMetricBaselineTests(unittest.TestCase):
    def test_skips_failed_snapshot_and_never_reads_future_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            revision = "a" * 64
            store_edinet_metrics(
                db,
                date(2026, 5, 7),
                [
                    {
                        "ticker": "7203",
                        "source_doc_id": "S100PAST",
                        "investment_securities": 250,
                        "extractor_revision": revision,
                    }
                ],
            )
            store_edinet_metrics(
                db,
                date(2026, 5, 8),
                [],
                status="failed",
                error="transient",
            )
            store_edinet_metrics(
                db,
                date(2026, 5, 9),
                [
                    {
                        "ticker": "7203",
                        "source_doc_id": "S100FUTURE",
                        "extractor_revision": revision,
                    }
                ],
            )

            baseline = read_edinet_metric_baseline(db, date(2026, 5, 8))

            self.assertIsNotNone(baseline)
            assert baseline is not None
            self.assertEqual(baseline.asof_date, date(2026, 5, 7))
            self.assertEqual(baseline.rows["7203"].record.source_doc_id, "S100PAST")
            self.assertEqual(baseline.rows["7203"].record.investment_securities, 250.0)

    def test_preserves_legacy_null_revision_as_ineligible_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_edinet_metrics(
                db,
                date(2026, 5, 8),
                [{"ticker": "7203", "extractor_revision": "a" * 64}],
            )
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE edinet_metrics SET extractor_revision = NULL")

            baseline = read_edinet_metric_baseline(db, date(2026, 5, 8))

            self.assertIsNotNone(baseline)
            assert baseline is not None
            self.assertIsNone(baseline.rows["7203"].extractor_revision)

    def test_rejects_ok_baseline_with_hard_parser_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_edinet_metrics(
                db,
                date(2026, 5, 8),
                [
                    {
                        "ticker": "7203",
                        "failure_reasons": ["csv_parse_failed"],
                        "extractor_revision": "a" * 64,
                    }
                ],
                status="ok",
            )

            with self.assertRaisesRegex(
                EDINETMetricBaselineError,
                "hard parser failure",
            ):
                read_edinet_metric_baseline(db, date(2026, 5, 8))

    def test_rejects_latest_ok_coverage_integrity_mismatches(self) -> None:
        corruptions = (
            ("coverage_start", "2026-05-07"),
            ("coverage_end", "2026-05-09"),
            ("error", "unexpected"),
            ("record_count", 0),
            ("record_count", 2),
        )
        for column, value in corruptions:
            with self.subTest(column=column, value=value), tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "market.sqlite"
                store_edinet_metrics(
                    db,
                    date(2026, 5, 8),
                    [{"ticker": "7203", "extractor_revision": "a" * 64}],
                )
                with sqlite3.connect(db) as connection:
                    connection.execute(
                        f"UPDATE source_coverage SET {column} = ? "  # nosec B608
                        "WHERE source = 'edinet_metrics'",
                        (value,),
                    )

                with self.assertRaises(EDINETMetricBaselineError):
                    read_edinet_metric_baseline(db, date(2026, 5, 8))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
