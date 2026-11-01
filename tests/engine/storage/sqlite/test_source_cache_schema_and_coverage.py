"""実行用source cacheのschemaと取得範囲の契約。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.storage.sqlite.source_cache.coverage import (
    covered_intervals,
    range_covered,
    record_range_source_coverage,
)
from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    open_connection,
)


class SourceCacheSchemaTests(unittest.TestCase):
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

            with self.assertRaisesRegex(SQLiteSchemaError, "obsolete market SQLite schema"):
                open_connection(db)


class DailyBarsCoverageTest(unittest.TestCase):
    def test_the_ten_day_imperial_transition_closure_uses_successful_empty_fetches(self) -> None:
        # The closure is reusable because its dates were fetched, not because
        # the surrounding prices happen to be close together.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            try:
                for start, end in (
                    (date(2019, 4, 22), date(2019, 4, 26)),
                    (date(2019, 4, 27), date(2019, 5, 6)),
                    (date(2019, 5, 7), date(2019, 5, 11)),
                ):
                    record_range_source_coverage(
                        conn,
                        source="jquants_daily_bars",
                        operation="get_daily_quotes",
                        table="jquants_daily_bars",
                        date_column="traded_at",
                        requested_start=start,
                        requested_end=end,
                        status="ok",
                        error=None,
                    )
                conn.commit()
                covered = range_covered(
                    conn, "jquants_daily_bars", date(2019, 4, 22), date(2019, 5, 11)
                )
                later_unfetched = range_covered(
                    conn, "jquants_daily_bars", date(2019, 4, 22), date(2019, 6, 30)
                )
            finally:
                conn.close()
            self.assertTrue(covered)
            self.assertFalse(later_unfetched)

    def test_a_fetch_chunk_holding_one_trading_day_is_not_covered_without_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            try:
                start, end = date(2024, 4, 1), date(2024, 5, 1)
                conn.execute(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
                    ("7203", (start + timedelta(days=15)).isoformat(), 1000.0),
                )
                conn.commit()
                sparse = range_covered(conn, "jquants_daily_bars", start, end)
                record_range_source_coverage(
                    conn,
                    source="jquants_daily_bars",
                    operation="get_daily_quotes",
                    table="jquants_daily_bars",
                    date_column="traded_at",
                    requested_start=start,
                    requested_end=end,
                    status="ok",
                    error=None,
                )
                conn.commit()
                after_fetch = range_covered(conn, "jquants_daily_bars", start, end)
            finally:
                conn.close()
            self.assertFalse(sparse)
            self.assertTrue(after_fetch)


class CoverageReadErrorsTest(unittest.TestCase):
    def test_unwritten_store_has_no_coverage(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            self.assertEqual(covered_intervals(connection, "jquants_fin_summaries"), ())
            self.assertFalse(
                range_covered(connection, "jquants_daily_bars", date(2026, 5, 1), date(2026, 5, 31))
            )

    def test_invalid_coverage_schema_is_not_an_unfetched_range(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE source_coverage (source TEXT)")
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such column"):
                covered_intervals(connection, "jquants_fin_summaries")

    def test_invalid_daily_coverage_schema_is_not_an_unfetched_range(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE source_coverage (source TEXT)")
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such column"):
                range_covered(connection, "jquants_daily_bars", date(2026, 5, 1), date(2026, 5, 31))
