from __future__ import annotations

import io
import json
import os
import shutil
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
    is_sqlite_stale,
    open_connection,
    rebuild_from_raw,
    refresh_from_raw,
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
                self.assertIn("jquants_earnings_calendar", tables)
                self.assertIn("jquants_market_calendar", tables)
                self.assertIn("edinet_documents", tables)
                self.assertIn("edinet_metrics", tables)
                self.assertIn("jpx_regulation_flags", tables)
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
                table_count = conn.execute(
                    "SELECT value FROM cache_metadata WHERE key = ?",
                    ("table_count.jquants_daily_bars",),
                ).fetchone()
            self.assertEqual(len(rows), 1)
            source, path, record_count, min_date, max_date = rows[0]
            self.assertEqual(source, "jquants_daily_bars")
            self.assertEqual(path, bars_path.as_posix())
            self.assertEqual(record_count, 1)
            self.assertEqual(min_date, "2024-03-19")
            self.assertEqual(max_date, "2024-03-19")
            self.assertEqual(table_count, ("1",))

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
            _write_json(raw / "jquants" / "get_unknown_endpoint.json", [])

            summary = rebuild_from_raw(raw, db)

            self.assertEqual(summary.skipped_files, ("get_unknown_endpoint.json",))

    def test_imports_earnings_calendar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "jquants" / "get_eq_earnings_cal.json",
                [
                    {"Code": "13010", "Date": "2026-05-15T00:00:00"},
                    {"Code": "13015", "Date": "2026-05-15T00:00:00"},  # non-common, dropped
                ],
            )

            summary = rebuild_from_raw(raw, db)

            self.assertEqual(summary.earnings_calendar_files, 1)
            self.assertEqual(summary.earnings_calendar_rows, 1)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT announcement_date, ticker FROM jquants_earnings_calendar"
                ).fetchall()
                self.assertEqual(rows, [("2026-05-15", "1301")])

    def test_imports_market_calendar_treats_division_2_as_business_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "jquants" / "get_mkt_calendar.json",
                [
                    {"Date": "2026-05-15", "HolidayDivision": "1"},
                    {"Date": "2026-12-30", "HolidayDivision": "2"},
                    {"Date": "2026-05-16", "HolidayDivision": "0"},
                ],
            )

            summary = rebuild_from_raw(raw, db)

            self.assertEqual(summary.market_calendar_rows, 3)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT day, is_business_day FROM jquants_market_calendar ORDER BY day"
                ).fetchall()
                self.assertEqual(
                    rows,
                    [("2026-05-15", 1), ("2026-05-16", 0), ("2026-12-30", 1)],
                )

    def test_imports_edinet_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "edinet" / "documents" / "2026-04-24.json",
                [
                    {"docID": "S100ABCD", "secCode": "13010", "docTypeCode": "120"},
                    {"docID": "S100ABCE", "secCode": "13020", "docTypeCode": "140"},
                ],
            )

            summary = rebuild_from_raw(raw, db)

            self.assertEqual(summary.edinet_document_rows, 2)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT doc_date, doc_id, sec_code, doc_type_code FROM edinet_documents "
                    "ORDER BY doc_id"
                ).fetchall()
                self.assertEqual(
                    rows,
                    [
                        ("2026-04-24", "S100ABCD", "13010", "120"),
                        ("2026-04-24", "S100ABCE", "13020", "140"),
                    ],
                )

    def test_imports_edinet_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "edinet" / "metrics" / "2026-04-24.json",
                [
                    {
                        "secCode": "13010",
                        "sales_ttm": 1_000_000.0,
                        "ocf_ttm": 200_000.0,
                        "debt": 50_000.0,
                        "cash": 80_000.0,
                        "ebitda_ttm": 300_000.0,
                        "consolidation_basis": "consolidated",
                        "ttm_quality_ev_ebitda": "exact",
                        "ttm_quality_p_s": "exact",
                        "ttm_quality_pcfr": "approximated",
                        "source_submit_datetime": "2026-04-01 12:00",
                        "source_period_start": "2025-04-01",
                        "source_period_end": "2026-03-31",
                    }
                ],
            )

            summary = rebuild_from_raw(raw, db)

            self.assertEqual(summary.edinet_metric_rows, 1)
            with sqlite3.connect(db) as conn:
                row = conn.execute(
                    "SELECT asof_date, ticker, sales_ttm, ttm_quality_ev_ebitda, "
                    "ttm_quality_pcfr, source_submit_datetime, source_period_start, "
                    "source_period_end FROM edinet_metrics"
                ).fetchone()
                self.assertEqual(
                    row,
                    (
                        "2026-04-24",
                        "1301",
                        1_000_000.0,
                        "exact",
                        "approximated",
                        "2026-04-01 12:00",
                        "2025-04-01",
                        "2026-03-31",
                    ),
                )

    def test_imports_jpx_regulation_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "jpx" / "regulations" / "2026-04-24.json",
                {
                    "schema_version": "v1",
                    "fetched_at_utc": "2026-04-24T03:00:00+00:00",
                    "flags_by_ticker": {
                        "13010": ["特別注意銘柄", "整理銘柄"],
                        "13020": ["取引停止"],
                    },
                    "source_names": ["特別注意銘柄", "整理銘柄", "取引停止"],
                },
            )

            summary = rebuild_from_raw(raw, db)

            self.assertEqual(summary.jpx_regulation_rows, 3)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT asof_date, source_name, ticker, flag, fetched_at_utc "
                    "FROM jpx_regulation_flags ORDER BY ticker, flag"
                ).fetchall()
                self.assertEqual(
                    rows,
                    [
                        ("2026-04-24", "整理銘柄", "1301", "整理銘柄", "2026-04-24T03:00:00+00:00"),
                        (
                            "2026-04-24",
                            "特別注意銘柄",
                            "1301",
                            "特別注意銘柄",
                            "2026-04-24T03:00:00+00:00",
                        ),
                        ("2026-04-24", "取引停止", "1302", "取引停止", "2026-04-24T03:00:00+00:00"),
                    ],
                )
                source_rows = conn.execute(
                    "SELECT asof_date, source_name, fetched_at_utc "
                    "FROM jpx_regulation_sources ORDER BY source_name"
                ).fetchall()
                self.assertEqual(
                    source_rows,
                    [
                        ("2026-04-24", "取引停止", "2026-04-24T03:00:00+00:00"),
                        ("2026-04-24", "整理銘柄", "2026-04-24T03:00:00+00:00"),
                        ("2026-04-24", "特別注意銘柄", "2026-04-24T03:00:00+00:00"),
                    ],
                )


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
            self.assertIn("jquants_master_snapshots: 1 files / 1 rows", stdout.getvalue())


class IsSqliteStaleTests(unittest.TestCase):
    def test_missing_db_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            raw.mkdir()
            db = Path(tmp) / "cache" / "market.sqlite"
            self.assertTrue(is_sqlite_stale([raw], db))

    def test_db_newer_than_raw_is_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "jquants" / "get_eq_master.json",
                [_master_record("13010", name="極洋", sector="水産・農林業")],
            )
            rebuild_from_raw(raw, db)
            self.assertFalse(is_sqlite_stale([raw], db))

    def test_db_older_than_raw_is_stale(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "jquants" / "get_eq_master.json",
                [_master_record("13010", name="極洋", sector="水産・農林業")],
            )
            rebuild_from_raw(raw, db)
            # touch raw to be newer than db
            time.sleep(0.05)
            new_raw = (
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-19.json"
            )
            _write_json(
                new_raw, [_bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0)]
            )
            os.utime(new_raw, (time.time() + 60, time.time() + 60))
            self.assertTrue(is_sqlite_stale([raw], db))

    def test_manifest_files_do_not_make_sqlite_stale(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "jquants" / "get_eq_master.json",
                [_master_record("13010", name="極洋", sector="水産・農林業")],
            )
            rebuild_from_raw(raw, db)
            manifest = raw / "manifests" / "screening-20260501-test.json"
            _write_json(manifest, {"run_id": "screening-20260501-test"})
            os.utime(manifest, (time.time() + 60, time.time() + 60))
            self.assertFalse(is_sqlite_stale([raw], db))

    def test_multiple_raw_dirs_checked(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "primary"
            secondary = Path(tmp) / "secondary"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                primary / "jquants" / "get_eq_master.json",
                [_master_record("13010", name="極洋", sector="水産・農林業")],
            )
            rebuild_from_raw(primary, db)
            self.assertFalse(is_sqlite_stale([primary, secondary], db))
            # add a fresh file to the secondary dir
            new_file = (
                secondary
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-19.json"
            )
            _write_json(
                new_file, [_bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0)]
            )
            os.utime(new_file, (time.time() + 60, time.time() + 60))
            self.assertTrue(is_sqlite_stale([primary, secondary], db))

    def test_missing_raw_dirs_not_stale_when_db_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "existing"
            absent = Path(tmp) / "absent"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                existing / "jquants" / "get_eq_master.json",
                [_master_record("13010", name="極洋", sector="水産・農林業")],
            )
            rebuild_from_raw(existing, db)
            self.assertFalse(is_sqlite_stale([existing, absent], db))

    def test_changed_raw_with_old_mtime_is_stale(self) -> None:
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
            old_time = db.stat().st_mtime - 60
            os.utime(bars_path, (old_time, old_time))

            self.assertTrue(is_sqlite_stale([raw], db))

    def test_deleted_imported_raw_is_stale(self) -> None:
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
            bars_path.unlink()

            self.assertTrue(is_sqlite_stale([raw], db))

    def test_new_raw_with_old_mtime_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "jquants" / "get_eq_master.json",
                [_master_record("13010", name="極洋", sector="水産・農林業")],
            )
            rebuild_from_raw(raw, db)
            new_raw = (
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-19.json"
            )
            _write_json(
                new_raw, [_bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0)]
            )
            old_time = db.stat().st_mtime - 60
            os.utime(new_raw, (old_time, old_time))

            self.assertTrue(is_sqlite_stale([raw], db))

    def test_table_count_mismatch_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-19.json",
                [_bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0)],
            )
            rebuild_from_raw(raw, db)
            with sqlite3.connect(db) as conn:
                conn.execute("DELETE FROM jquants_daily_bars")
                conn.commit()

            self.assertTrue(is_sqlite_stale([raw], db))

    def test_relative_and_absolute_raw_dirs_match_same_cache_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "jquants" / "get_eq_master.json",
                [_master_record("13010", name="極洋", sector="水産・農林業")],
            )
            rebuild_from_raw(raw.resolve(), db)

            raw_relative = Path(os.path.relpath(raw, Path.cwd()))

            self.assertFalse(is_sqlite_stale([raw_relative], db))


class RefreshFromRawTests(unittest.TestCase):
    def test_imports_only_new_raw_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-19.json",
                [_bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0)],
            )
            rebuild_from_raw(raw, db)

            _write_json(
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-05-19-start_dt-2024-04-19.json",
                [_bars_record("13010", "2024-04-19", close=110.0, adj_close=110.0)],
            )

            summary = refresh_from_raw(raw, db)

            self.assertEqual(summary.daily_bars_files, 1)
            self.assertEqual(summary.daily_bars_rows, 1)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT traded_at, close FROM jquants_daily_bars ORDER BY traded_at"
                ).fetchall()
            self.assertEqual(rows, [("2024-03-19", 100.0), ("2024-04-19", 110.0)])

    def test_noops_when_every_raw_file_is_already_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "jquants" / "get_eq_master.json",
                [_master_record("13010", name="極洋", sector="水産・農林業")],
            )
            rebuild_from_raw(raw, db)

            summary = refresh_from_raw(raw, db)

            self.assertEqual(summary.master_files, 0)
            self.assertEqual(summary.master_rows, 0)

    def test_corrupt_existing_table_falls_back_to_full_rebuild_before_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            first_path = (
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-03-20-start_dt-2024-03-19.json"
            )
            second_path = (
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-20-start_dt-2024-04-19.json"
            )
            _write_json(
                first_path,
                [
                    _bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0),
                    _bars_record("13010", "2024-03-20", close=101.0, adj_close=101.0),
                ],
            )
            rebuild_from_raw(raw, db)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "DELETE FROM jquants_daily_bars WHERE traded_at = ?",
                    ("2024-03-20",),
                )
                conn.commit()
            _write_json(
                second_path,
                [_bars_record("13010", "2024-04-19", close=110.0, adj_close=110.0)],
            )

            summary = refresh_from_raw(raw, db)

            self.assertEqual(summary.daily_bars_files, 2)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT traded_at, close FROM jquants_daily_bars ORDER BY traded_at"
                ).fetchall()
            self.assertEqual(
                rows,
                [("2024-03-19", 100.0), ("2024-03-20", 101.0), ("2024-04-19", 110.0)],
            )

    def test_changed_existing_raw_file_falls_back_to_full_rebuild(self) -> None:
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
                [
                    _bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0),
                    _bars_record("13010", "2024-03-20", close=101.0, adj_close=101.0),
                ],
            )
            rebuild_from_raw(raw, db)

            _write_json(
                bars_path,
                [_bars_record("13010", "2024-03-19", close=200.0, adj_close=200.0)],
            )
            # Keep an older mtime than the DB. Refresh must still compare SHA
            # and rebuild; relying on mtime would leave the stale 2024-03-20 row.
            db_mtime = db.stat().st_mtime
            os_time = db_mtime - 60
            bars_path.touch()

            os.utime(bars_path, (os_time, os_time))

            summary = refresh_from_raw(raw, db)

            self.assertEqual(summary.daily_bars_files, 1)
            self.assertEqual(summary.daily_bars_rows, 1)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT traded_at, close FROM jquants_daily_bars ORDER BY traded_at"
                ).fetchall()
            self.assertEqual(rows, [("2024-03-19", 200.0)])

    def test_deleted_imported_raw_file_falls_back_to_full_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            first_path = (
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-19.json"
            )
            second_path = (
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-05-19-start_dt-2024-04-19.json"
            )
            _write_json(
                first_path,
                [_bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0)],
            )
            _write_json(
                second_path,
                [_bars_record("13010", "2024-04-19", close=110.0, adj_close=110.0)],
            )
            rebuild_from_raw(raw, db)
            first_path.unlink()

            summary = refresh_from_raw(raw, db)

            self.assertEqual(summary.daily_bars_files, 1)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT traded_at, close FROM jquants_daily_bars ORDER BY traded_at"
                ).fetchall()
            self.assertEqual(rows, [("2024-04-19", 110.0)])

    def test_imported_path_outside_current_roots_falls_back_to_full_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "primary"
            secondary = Path(tmp) / "secondary"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                primary
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-03-20-start_dt-2024-03-19.json",
                [_bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0)],
            )
            _write_json(
                secondary
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-03-22-start_dt-2024-03-21.json",
                [_bars_record("13010", "2024-03-21", close=110.0, adj_close=110.0)],
            )
            rebuild_from_raw([primary, secondary], db)

            summary = refresh_from_raw(primary, db)

            self.assertEqual(summary.daily_bars_files, 1)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT traded_at, close FROM jquants_daily_bars ORDER BY traded_at"
                ).fetchall()
            self.assertEqual(rows, [("2024-03-19", 100.0)])

    def test_missing_raw_root_falls_back_to_full_rebuild_without_stale_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "primary"
            secondary = Path(tmp) / "secondary"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                primary
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-03-20-start_dt-2024-03-19.json",
                [_bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0)],
            )
            _write_json(
                secondary
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-03-22-start_dt-2024-03-21.json",
                [_bars_record("13010", "2024-03-21", close=110.0, adj_close=110.0)],
            )
            rebuild_from_raw([primary, secondary], db)
            shutil.rmtree(secondary)

            summary = refresh_from_raw([primary, secondary], db)

            self.assertEqual(summary.daily_bars_files, 1)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT traded_at, close FROM jquants_daily_bars ORDER BY traded_at"
                ).fetchall()
            self.assertEqual(rows, [("2024-03-19", 100.0)])

    def test_overlapping_new_chunk_matches_full_rebuild_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            later_chunk = (
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-05-19-start_dt-2024-04-19.json"
            )
            earlier_chunk = (
                raw
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-30-start_dt-2024-04-01.json"
            )
            _write_json(
                later_chunk,
                [_bars_record("13010", "2024-04-25", close=200.0, adj_close=200.0)],
            )
            rebuild_from_raw(raw, db)
            _write_json(
                earlier_chunk,
                [_bars_record("13010", "2024-04-25", close=100.0, adj_close=100.0)],
            )

            summary = refresh_from_raw(raw, db)

            self.assertEqual(summary.daily_bars_files, 2)
            with sqlite3.connect(db) as conn:
                row = conn.execute(
                    "SELECT close FROM jquants_daily_bars WHERE ticker = '1301' "
                    "AND traded_at = '2024-04-25'"
                ).fetchone()
            self.assertEqual(row, (200.0,))

    def test_overlapping_new_chunks_across_roots_match_full_rebuild_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "primary"
            secondary = Path(tmp) / "secondary"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                primary
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-30-start_dt-2024-04-01.json",
                [_bars_record("13010", "2024-04-25", close=100.0, adj_close=100.0)],
            )
            _write_json(
                secondary
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-05-19-start_dt-2024-04-19.json",
                [_bars_record("13010", "2024-04-25", close=200.0, adj_close=200.0)],
            )
            conn = open_connection(db)
            conn.close()

            summary = refresh_from_raw([secondary, primary], db)

            self.assertEqual(summary.daily_bars_files, 2)
            with sqlite3.connect(db) as conn:
                row = conn.execute(
                    "SELECT close FROM jquants_daily_bars WHERE ticker = '1301' "
                    "AND traded_at = '2024-04-25'"
                ).fetchone()
            self.assertEqual(row, (100.0,))

    def test_archive_like_nested_files_are_ignored_by_refresh_staleness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                raw / "jquants" / "get_eq_master.json",
                [_master_record("13010", name="極洋", sector="水産・農林業")],
            )
            rebuild_from_raw(raw, db)
            _write_json(
                raw / "archive" / "jquants" / "get_eq_master.json",
                [_master_record("13020", name="Archive", sector="水産・農林業")],
            )

            self.assertFalse(is_sqlite_stale([raw], db))


class SectorNameNormalizationTests(unittest.TestCase):
    def test_master_import_normalizes_halfwidth_middle_dot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            db = Path(tmp) / "cache" / "market.sqlite"
            # J-Quants は同じ TSE 33 セクターを半角中黒 (U+FF65) で返してくる
            # ことがある。SQLite 取り込み段階で全角形に正規化されないと、
            # outlook の `情報・通信業` (全角) と一致せず select で sector=null
            # になり、supportive 分類が漏れる。
            _write_json(
                raw / "jquants" / "get_eq_master.json",
                [
                    {
                        "Code": "47160",
                        "CoName": "日本オラクル",
                        "MktNm": "プライム",
                        "S33Nm": "情報･通信業",  # half-width middle dot
                        "Mrgn": "2",
                        "Date": "2026-05-07T00:00:00",
                    },
                ],
            )

            rebuild_from_raw(raw, db)
            with sqlite3.connect(db) as conn:
                row = conn.execute(
                    "SELECT sector_33 FROM jquants_master_snapshots WHERE ticker = '4716'"
                ).fetchone()
            self.assertEqual(row[0], "情報・通信業")  # full-width, canonical


class RebuildFromMultipleDirsTests(unittest.TestCase):
    def test_merges_records_across_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "primary"
            secondary = Path(tmp) / "secondary"
            db = Path(tmp) / "cache" / "market.sqlite"
            _write_json(
                primary
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-19.json",
                [_bars_record("13010", "2024-03-19", close=100.0, adj_close=100.0)],
            )
            _write_json(
                secondary
                / "jquants"
                / "get_eq_bars_daily_range-end_dt-2024-05-19-start_dt-2024-04-19.json",
                [_bars_record("13010", "2024-04-19", close=110.0, adj_close=110.0)],
            )

            summary = rebuild_from_raw([primary, secondary], db)

            self.assertEqual(summary.daily_bars_files, 2)
            self.assertEqual(summary.daily_bars_rows, 2)
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT traded_at, close FROM jquants_daily_bars ORDER BY traded_at"
                ).fetchall()
                self.assertEqual(rows, [("2024-03-19", 100.0), ("2024-04-19", 110.0)])


if __name__ == "__main__":  # pragma: no cover
    assert issubclass(SQLiteCacheError, RuntimeError)
    unittest.main()
