from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.cli import rebuild_cache_command
from baibai_loop.screening.sqlite_cache import (
    SCHEMA_VERSION,
    SQLiteCacheError,
    open_connection,
    rebuild_from_raw,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _bars_record(code: str, date_iso: str, *, close: float, adj_close: float) -> dict[str, object]:
    return {
        "Code": code,
        "Date": f"{date_iso}T00:00:00",
        "O": close - 1.0,
        "H": close + 1.0,
        "L": close - 2.0,
        "C": close,
        "Vo": 1000.0,
        "Va": 100000.0,
        "AdjO": close - 1.0,
        "AdjH": close + 1.0,
        "AdjL": close - 2.0,
        "AdjC": adj_close,
        "AdjVo": 1000.0,
        "AdjFactor": 1.0,
        "UL": "0",
        "LL": "0",
    }


def _fin_summary_record(code: str, disclosed_iso: str, *, eps: float | str) -> dict[str, object]:
    return {
        "Code": code,
        "DiscDate": f"{disclosed_iso}T00:00:00",
        "FEPS": "",
        "EPS": eps,
        "BPS": "",
        "AvgSh": 1_000_000.0,
        "Sales": 5_000_000.0,
        "OP": 1_000_000.0,
        "OdP": 950_000.0,
        "NP": 700_000.0,
        "CurPerType": "2Q",
        "CurFYEn": "2026-03-31T00:00:00",
        "CurPerSt": "2025-04-01T00:00:00",
        "CurPerEn": "2025-09-30T00:00:00",
    }


def _master_record(code: str, *, name: str, sector: str) -> dict[str, object]:
    return {
        "Code": code,
        "Date": "2026-05-07T00:00:00",
        "CoName": name,
        "MktNm": "プライム",
        "S33Nm": sector,
        "Mrgn": "2",
    }


class OpenConnectionTests(unittest.TestCase):
    def test_creates_schema_and_records_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(db_path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("jquants_daily_bars", tables)
                self.assertIn("jquants_fin_summaries", tables)
                self.assertIn("jquants_master_snapshots", tables)
                self.assertIn("raw_imports", tables)
                self.assertIn("cache_metadata", tables)
                version = conn.execute(
                    "SELECT value FROM cache_metadata WHERE key = 'schema_version'"
                ).fetchone()
                self.assertEqual(version[0], SCHEMA_VERSION)
            finally:
                conn.close()


class RebuildFromRawTests(unittest.TestCase):
    def test_imports_daily_bars_with_short_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-19.json",
                [
                    _bars_record("13010", "2024-03-19", close=3790.0, adj_close=3790.0),
                    _bars_record("13010", "2024-03-21", close=3800.0, adj_close=3800.0),
                ],
            )

            summary = rebuild_from_raw(raw, db)

            self.assertEqual(summary.daily_bars_files, 1)
            self.assertEqual(summary.daily_bars_rows, 2)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT ticker, traded_at, close, adjustment_close FROM jquants_daily_bars "
                    "ORDER BY traded_at"
                ).fetchall()
                self.assertEqual(
                    rows,
                    [
                        ("1301", "2024-03-19", 3790.0, 3790.0),
                        ("1301", "2024-03-21", 3800.0, 3800.0),
                    ],
                )

    def test_drops_non_common_stock_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-19.json",
                [
                    _bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0),
                    _bars_record("13015", "2024-03-19", close=200.0, adj_close=200.0),
                ],
            )

            summary = rebuild_from_raw(raw, db)

            self.assertEqual(summary.daily_bars_rows, 1)
            with sqlite3.connect(db) as conn:
                rows = conn.execute("SELECT ticker FROM jquants_daily_bars").fetchall()
                self.assertEqual(rows, [("1301",)])

    def test_imports_fin_summary_and_master(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw
                / "jquants"
                / "get_fin_summary_range-end_dt-2025-10-28-start_dt-2025-09-28.json",
                [_fin_summary_record("34470", "2025-09-29", eps="50.0")],
            )
            _write_json(
                raw / "jquants" / "get_eq_master.json",
                [_master_record("13010", name="極洋", sector="水産・農林業")],
            )

            summary = rebuild_from_raw(raw, db)

            self.assertEqual(summary.fin_summary_rows, 1)
            self.assertEqual(summary.master_rows, 1)
            with sqlite3.connect(db) as conn:
                fin = conn.execute(
                    "SELECT ticker, disclosed_at, eps_ttm, fiscal_period FROM jquants_fin_summaries"
                ).fetchall()
                self.assertEqual(fin, [("3447", "2025-09-29", 50.0, "2Q")])
                master = conn.execute(
                    "SELECT snapshot_date, ticker, name, sector_33, is_common_stock "
                    "FROM jquants_master_snapshots"
                ).fetchall()
                self.assertEqual(
                    master,
                    [("2026-05-07", "1301", "極洋", "水産・農林業", 1)],
                )

    def test_records_raw_imports_with_sha256_and_record_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            bars_path = (
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-19.json"
            )
            _write_json(
                bars_path,
                [_bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0)],
            )

            rebuild_from_raw(raw, db)

            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT source, path, record_count, min_date, max_date FROM raw_imports"
                ).fetchall()
            self.assertEqual(len(rows), 1)
            source, path, record_count, min_date, max_date = rows[0]
            self.assertEqual(source, "jquants_daily_bars")
            self.assertEqual(path, bars_path.as_posix())
            self.assertEqual(record_count, 1)
            self.assertEqual(min_date, "2024-03-19")
            self.assertEqual(max_date, "2024-03-19")

    def test_rebuild_is_idempotent_and_replaces_old_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            bars_path = (
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-19.json"
            )
            _write_json(
                bars_path,
                [_bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0)],
            )
            rebuild_from_raw(raw, db)

            _write_json(
                bars_path,
                [_bars_record("13010", "2024-03-19", close=200.0, adj_close=200.0)],
            )
            rebuild_from_raw(raw, db)

            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT close FROM jquants_daily_bars WHERE ticker = '1301'"
                ).fetchall()
            self.assertEqual(rows, [(200.0,)])

    def test_unrecognized_files_are_reported_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(raw / "jquants" / "get_mkt_calendar.json", [])

            summary = rebuild_from_raw(raw, db)

            self.assertEqual(summary.skipped_files, ("get_mkt_calendar.json",))


class RebuildCacheCommandTests(unittest.TestCase):
    def test_returns_one_when_raw_dir_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            exit_code = rebuild_cache_command(
                raw_dir=Path(tmp) / "missing",
                sqlite_path=Path(tmp) / "out.sqlite",
                stdout=stdout,
            )
            self.assertEqual(exit_code, 1)

    def test_reports_summary_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "jquants" / "get_eq_master.json",
                [_master_record("13010", name="極洋", sector="水産・農林業")],
            )

            stdout = io.StringIO()
            exit_code = rebuild_cache_command(raw_dir=raw, sqlite_path=db, stdout=stdout)

            self.assertEqual(exit_code, 0)
            self.assertIn("1 master files / 1 rows", stdout.getvalue())


if __name__ == "__main__":  # pragma: no cover
    assert issubclass(SQLiteCacheError, RuntimeError)
    unittest.main()
