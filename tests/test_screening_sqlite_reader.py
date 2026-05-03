from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.sqlite_cache import open_connection
from baibai_loop.screening.sqlite_reader import (
    read_daily_bars,
    read_eq_master,
    read_fin_summaries,
)


def _add_raw_import(
    conn: sqlite3.Connection,
    *,
    source: str,
    path: str,
    record_count: int,
    min_date: str | None,
    max_date: str | None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO raw_imports("
        "source, path, sha256, imported_at_utc, record_count, min_date, max_date"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, path, "0" * 64, datetime.now(UTC).isoformat(), record_count, min_date, max_date),
    )


class ReadEqMasterTests(unittest.TestCase):
    def test_returns_none_when_sqlite_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_eq_master(Path(tmp) / "missing.sqlite"))

    def test_returns_none_when_no_master_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            conn.commit()
            conn.close()
            self.assertIsNone(read_eq_master(db))

    def test_returns_securities_after_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            conn.execute(
                "INSERT INTO jquants_master_snapshots("
                "snapshot_date, ticker, name, market, sector_33, is_common_stock, raw_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-07", "1301", "極洋", "プライム", "水産・農林業", 1, "{}"),
            )
            _add_raw_import(
                conn,
                source="jquants_master_snapshots",
                path="data/raw/screening/jquants/get_eq_master.json",
                record_count=1,
                min_date="2026-05-07",
                max_date="2026-05-07",
            )
            conn.commit()
            conn.close()

            masters = read_eq_master(db)
            self.assertIsNotNone(masters)
            assert masters is not None  # narrow for type checker
            self.assertEqual(len(masters), 1)
            self.assertEqual(masters[0].code, "1301")
            self.assertEqual(masters[0].name, "極洋")
            self.assertEqual(masters[0].sector_33, "水産・農林業")
            self.assertTrue(masters[0].is_common_stock)

    def test_picks_latest_snapshot_when_ticker_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            conn.executemany(
                "INSERT INTO jquants_master_snapshots("
                "snapshot_date, ticker, name, market, sector_33, is_common_stock, raw_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("2026-04-07", "1301", "OldName", "プライム", "水産", 1, "{}"),
                    ("2026-05-07", "1301", "NewName", "プライム", "水産", 1, "{}"),
                ],
            )
            _add_raw_import(
                conn,
                source="jquants_master_snapshots",
                path="m.json",
                record_count=2,
                min_date="2026-04-07",
                max_date="2026-05-07",
            )
            conn.commit()
            conn.close()

            masters = read_eq_master(db)
            assert masters is not None
            self.assertEqual(masters[0].name, "NewName")


class ReadDailyBarsTests(unittest.TestCase):
    def test_returns_none_when_sqlite_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                read_daily_bars(Path(tmp) / "missing.sqlite", date(2024, 3, 19), date(2024, 4, 18))
            )

    def test_returns_none_when_range_not_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            _add_raw_import(
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
                "ticker, traded_at, close, turnover_value, adjustment_close"
                ") VALUES (?, ?, ?, ?, ?)",
                [
                    ("1301", "2024-03-19", 3790.0, 1000.0, 3790.0),
                    ("1301", "2024-03-21", 3800.0, 1200.0, 3800.0),
                    ("1301", "2024-04-19", 3900.0, 1500.0, 3900.0),
                ],
            )
            _add_raw_import(
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


class ReadFinSummariesTests(unittest.TestCase):
    def test_returns_summaries_in_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "market.sqlite"
            conn = open_connection(db)
            conn.execute(
                "INSERT INTO jquants_fin_summaries("
                "ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding, "
                "sales, operating_profit, ordinary_profit, profit, "
                "fiscal_period, fiscal_year_end, period_start, period_end, raw_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    "{}",
                ),
            )
            _add_raw_import(
                conn,
                source="jquants_fin_summaries",
                path="data/raw/screening/jquants/"
                "get_fin_summary_range-end_dt-2025-10-28-start_dt-2025-09-28.json",
                record_count=1,
                min_date="2025-09-29",
                max_date="2025-09-29",
            )
            conn.commit()
            conn.close()

            summaries = read_fin_summaries(db, date(2025, 9, 28), date(2025, 10, 28))
            assert summaries is not None
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].ticker, "3447")
            self.assertEqual(summaries[0].eps_ttm, 50.0)
            self.assertEqual(summaries[0].fiscal_period, "2Q")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
