from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from baibai_loop.screening.sqlite_cache import (
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    open_connection,
    store_edinet_documents,
    store_edinet_metrics,
    store_jpx_regulations,
    store_jquants_daily_bars,
    store_jquants_earnings_calendar,
    store_jquants_fin_summaries,
    store_jquants_market_calendar,
    store_jquants_master,
)
from baibai_loop.screening.sqlite_reader import _range_covered


class SQLiteCacheTest(unittest.TestCase):
    def test_creates_current_schema_with_user_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()
                tables = {
                    str(row[0])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
            finally:
                conn.close()

            self.assertEqual(version[0], SQLITE_SCHEMA_VERSION)
            self.assertIn("source_coverage", tables)
            self.assertNotIn("raw_imports", tables)
            self.assertNotIn("cache_metadata", tables)

    def test_rejects_legacy_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE source_coverage(source TEXT)")
                conn.execute("PRAGMA user_version = 9")
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(SQLiteSchemaError, "unsupported screening SQLite schema"):
                open_connection(db)

    def test_direct_store_writes_minimal_source_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            rows = store_jquants_daily_bars(
                db,
                [
                    {
                        "Code": "72030",
                        "Date": "2026-05-08",
                        "Close": 1000,
                        "AdjustmentClose": 1000,
                    }
                ],
                requested_start=date(2026, 5, 8),
                requested_end=date(2026, 5, 8),
            )
            self.assertEqual(rows, 1)
            conn = sqlite3.connect(db)
            try:
                coverage = conn.execute(
                    "SELECT source, coverage_key, record_count, status FROM source_coverage"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(
                coverage,
                (
                    "jquants_daily_bars",
                    "get_eq_bars_daily_range:2026-05-08..2026-05-08",
                    1,
                    "ok",
                ),
            )

    def test_daily_bars_source_coverage_counts_persisted_unique_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            rows = store_jquants_daily_bars(
                db,
                [
                    {
                        "Code": "72030",
                        "Date": "2026-05-08",
                        "Close": 1000,
                        "AdjustmentClose": 1000,
                    },
                    {
                        "Code": "72030",
                        "Date": "2026-05-08",
                        "Close": 1001,
                        "AdjustmentClose": 1001,
                    },
                ],
                requested_start=date(2026, 5, 8),
                requested_end=date(2026, 5, 8),
            )
            conn = sqlite3.connect(db)
            try:
                table_count = conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()
                coverage = conn.execute(
                    "SELECT record_count FROM source_coverage WHERE source = ?",
                    ("jquants_daily_bars",),
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(rows, 1)
            self.assertEqual(table_count[0], 1)
            self.assertEqual(coverage, (1,))

    def test_fin_summaries_source_coverage_counts_persisted_unique_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            rows = store_jquants_fin_summaries(
                db,
                [
                    {"Code": "72030", "DisclosedDate": "2026-05-08", "NetSales": 100},
                    {"Code": "72030", "DisclosedDate": "2026-05-08", "NetSales": 101},
                ],
                requested_start=date(2026, 5, 8),
                requested_end=date(2026, 5, 8),
            )
            conn = sqlite3.connect(db)
            try:
                table_count = conn.execute("SELECT COUNT(*) FROM jquants_fin_summaries").fetchone()
                coverage = conn.execute(
                    "SELECT record_count FROM source_coverage WHERE source = ?",
                    ("jquants_fin_summaries",),
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(rows, 1)
            self.assertEqual(table_count[0], 1)
            self.assertEqual(coverage, (1,))

    def test_fin_summaries_shifted_chunk_refetch_keeps_coverage_contiguous(self) -> None:
        """A later bootstrap anchors chunk boundaries at a new asof, so a re-fetch only
        partially overlaps an existing window. Recording it must merge into the union,
        not delete-and-shrink the window and orphan its earlier part -- otherwise
        `_range_covered` reports a gap even though the rows are still present.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [
                    {"Code": "72030", "DisclosedDate": "2026-05-13", "NetSales": 100},
                    {"Code": "72040", "DisclosedDate": "2026-05-27", "NetSales": 200},
                ],
                requested_start=date(2026, 5, 12),
                requested_end=date(2026, 5, 29),
            )
            store_jquants_fin_summaries(
                db,
                [
                    {"Code": "72040", "DisclosedDate": "2026-05-27", "NetSales": 200},
                    {"Code": "72050", "DisclosedDate": "2026-06-02", "NetSales": 300},
                ],
                requested_start=date(2026, 5, 19),
                requested_end=date(2026, 6, 5),
            )
            conn = sqlite3.connect(db)
            try:
                covered = _range_covered(
                    conn, "jquants_fin_summaries", date(2026, 5, 12), date(2026, 6, 5)
                )
                windows = conn.execute(
                    "SELECT coverage_start, coverage_end, record_count FROM source_coverage "
                    "WHERE source = 'jquants_fin_summaries' ORDER BY coverage_start"
                ).fetchall()
            finally:
                conn.close()
            self.assertTrue(covered)
            self.assertEqual(windows, [("2026-05-12", "2026-06-05", 3)])

    def test_daily_bars_shifted_chunk_refetch_merges_coverage_window(self) -> None:
        """The shared coverage-merge path keeps daily_bars source_coverage contiguous
        so its record-count consistency check stays satisfied after a re-fetch with
        shifted chunk boundaries."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_daily_bars(
                db,
                [
                    {"Code": "72030", "Date": "2026-05-12", "Close": 1000, "AdjustmentClose": 1000},
                    {"Code": "72030", "Date": "2026-05-27", "Close": 1100, "AdjustmentClose": 1100},
                ],
                requested_start=date(2026, 5, 12),
                requested_end=date(2026, 5, 29),
            )
            store_jquants_daily_bars(
                db,
                [
                    {"Code": "72030", "Date": "2026-05-27", "Close": 1100, "AdjustmentClose": 1100},
                    {"Code": "72030", "Date": "2026-06-02", "Close": 1200, "AdjustmentClose": 1200},
                ],
                requested_start=date(2026, 5, 19),
                requested_end=date(2026, 6, 5),
            )
            conn = sqlite3.connect(db)
            try:
                windows = conn.execute(
                    "SELECT coverage_start, coverage_end, record_count FROM source_coverage "
                    "WHERE source = 'jquants_daily_bars' ORDER BY coverage_start"
                ).fetchall()
                table_count = conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(windows, [("2026-05-12", "2026-06-05", 3)])
            self.assertEqual(table_count, 3)

    def test_snapshot_and_single_day_stores_count_persisted_unique_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"

            self.assertEqual(
                store_jquants_master(
                    db,
                    [
                        {
                            "Code": "72030",
                            "Date": "2026-05-08",
                            "CompanyName": "Toyota",
                            "MarketCodeName": "Prime",
                            "Sector33CodeName": "輸送用機器",
                        },
                        {
                            "Code": "72030",
                            "Date": "2026-05-08",
                            "CompanyName": "Toyota 2",
                            "MarketCodeName": "Prime",
                            "Sector33CodeName": "輸送用機器",
                        },
                    ],
                ),
                1,
            )
            self.assertEqual(
                store_jquants_earnings_calendar(
                    db,
                    [
                        {"Code": "72030", "Date": "2026-05-08"},
                        {"Code": "72030", "Date": "2026-05-08"},
                    ],
                    requested_start=date(2026, 5, 8),
                    requested_end=date(2026, 5, 8),
                ),
                1,
            )
            self.assertEqual(
                store_jquants_market_calendar(
                    db,
                    [
                        {"Date": "2026-05-08", "HolidayDivision": "1"},
                        {"Date": "2026-05-08", "HolidayDivision": "0"},
                    ],
                    requested_start=date(2026, 5, 8),
                    requested_end=date(2026, 5, 8),
                ),
                1,
            )
            self.assertEqual(
                store_edinet_documents(
                    db,
                    date(2026, 5, 8),
                    [
                        {"docID": "S100TEST", "secCode": "72030", "docTypeCode": "120"},
                        {"docID": "S100TEST", "secCode": "72030", "docTypeCode": "120"},
                    ],
                ),
                1,
            )
            self.assertEqual(
                store_edinet_metrics(
                    db,
                    date(2026, 5, 8),
                    [
                        {"ticker": "7203", "sales_ttm": 1, "failure_reasons": []},
                        {"ticker": "7203", "sales_ttm": 2, "failure_reasons": []},
                    ],
                ),
                1,
            )
            self.assertEqual(
                store_jpx_regulations(
                    db,
                    date(2026, 5, 8),
                    flags_by_ticker={"7203": ["特別注意銘柄", "特別注意銘柄"]},
                    source_names=["特別注意銘柄"],
                ),
                1,
            )

            conn = sqlite3.connect(db)
            try:
                coverage = dict(
                    conn.execute(
                        "SELECT source, record_count FROM source_coverage "
                        "WHERE source IN ("
                        "'jquants_master_snapshots', 'jquants_earnings_calendar', "
                        "'jquants_market_calendar', 'edinet_documents', 'edinet_metrics', "
                        "'jpx_regulation_flags')"
                    ).fetchall()
                )
            finally:
                conn.close()

            self.assertEqual(
                coverage,
                {
                    "jquants_master_snapshots": 1,
                    "jquants_earnings_calendar": 1,
                    "jquants_market_calendar": 1,
                    "edinet_documents": 1,
                    "edinet_metrics": 1,
                    "jpx_regulation_flags": 1,
                },
            )

    def test_direct_stores_do_not_persist_raw_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            store_jquants_fin_summaries(
                db,
                [{"Code": "72030", "DisclosedDate": "2026-05-08", "NetSales": 100}],
                requested_start=date(2026, 5, 8),
                requested_end=date(2026, 5, 8),
            )
            store_jquants_master(
                db,
                [
                    {
                        "Code": "72030",
                        "Date": "2026-05-08",
                        "CompanyName": "Toyota",
                        "MarketCodeName": "Prime",
                        "Sector33CodeName": "輸送用機器",
                    }
                ],
            )
            store_jquants_earnings_calendar(
                db,
                [{"Code": "72030", "Date": "2026-05-15"}],
                requested_start=date(2026, 5, 8),
                requested_end=date(2026, 8, 6),
            )
            store_jquants_market_calendar(
                db,
                [{"Date": "2026-05-08", "HolidayDivision": "1"}],
                requested_start=date(2026, 5, 8),
                requested_end=date(2026, 5, 8),
            )
            store_edinet_documents(
                db,
                date(2026, 5, 8),
                [
                    {
                        "docID": "S100TEST",
                        "secCode": "72030",
                        "docTypeCode": "120",
                        "csvFlag": "1",
                        "submitDateTime": "2026-05-08 15:00",
                    }
                ],
            )
            store_edinet_metrics(
                db,
                date(2026, 5, 8),
                [{"ticker": "7203", "sales_ttm": 1, "failure_reasons": []}],
            )
            store_jpx_regulations(
                db,
                date(2026, 5, 8),
                flags_by_ticker={"7203": ["特別注意銘柄"]},
                source_names=["特別注意銘柄"],
            )

            conn = sqlite3.connect(db)
            try:
                for table in (
                    "jquants_fin_summaries",
                    "jquants_master_snapshots",
                    "jquants_earnings_calendar",
                    "jquants_market_calendar",
                    "edinet_documents",
                ):
                    columns = [
                        str(row[1])
                        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                    ]
                    self.assertNotIn("raw_json", columns)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
