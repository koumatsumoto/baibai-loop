from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.providers.jquants import JQuantsProvider, JQuantsProviderError
from baibai_loop.screening.sqlite_cache import open_connection, store_jquants_daily_bars


class _RecordingClient:
    """Stand-in for jquantsapi.ClientV2 that records every call so tests can
    confirm the SQLite read-through path skipped the API entirely.
    """

    def __init__(self) -> None:
        self.eq_master_calls = 0
        self.bars_calls: list[tuple[str, str]] = []
        self.fin_calls: list[tuple[str, str]] = []

    def get_eq_master(self) -> list[dict[str, Any]]:
        self.eq_master_calls += 1
        return [
            {
                "Code": "13010",
                "CoName": "API_FALLBACK_NAME",
                "MktNm": "プライム",
                "S33Nm": "水産・農林業",
                "Mrgn": "2",
            }
        ]

    def get_eq_bars_daily_range(self, start_dt: str, end_dt: str) -> list[dict[str, Any]]:
        self.bars_calls.append((start_dt, end_dt))
        return [{"Code": "13010", "Date": f"{start_dt}T00:00:00", "C": 999.0, "Va": 0.0}]

    def get_fin_summary_range(self, start_dt: str, end_dt: str) -> list[dict[str, Any]]:
        self.fin_calls.append((start_dt, end_dt))
        return []


def _add_raw_import(
    conn: sqlite3.Connection,
    *,
    source: str,
    record_count: int,
    min_date: str,
    max_date: str,
    path: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO raw_imports("
        "source, path, sha256, imported_at_utc, record_count, min_date, max_date"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            path or f"records/_data/raw/screening/jquants/{source}.json",
            "0" * 64,
            datetime.now(UTC).isoformat(),
            record_count,
            min_date,
            max_date,
        ),
    )


class JQuantsProviderSQLiteReadThroughTests(unittest.TestCase):
    def test_get_eq_master_uses_sqlite_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.execute(
                "INSERT INTO jquants_master_snapshots("
                "snapshot_date, ticker, name, market, sector_33, is_common_stock, raw_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-07", "1301", "極洋", "プライム", "水産・農林業", 1, "{}"),
            )
            _add_raw_import(
                conn,
                source="jquants_master_snapshots",
                record_count=1,
                min_date="2026-05-07",
                max_date="2026-05-07",
            )
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            masters = provider.get_eq_master()

            self.assertEqual(client.eq_master_calls, 0)
            self.assertEqual(len(masters), 1)
            self.assertEqual(masters[0].name, "極洋")

    def test_get_eq_master_falls_back_to_api_when_sqlite_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            masters = provider.get_eq_master()

            self.assertEqual(client.eq_master_calls, 1)
            self.assertEqual(masters[0].name, "API_FALLBACK_NAME")

    def test_get_bars_uses_sqlite_for_covered_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.execute(
                "INSERT INTO jquants_daily_bars("
                "ticker, traded_at, close, turnover_value, adjustment_close"
                ") VALUES (?, ?, ?, ?, ?)",
                ("1301", "2024-03-19", 3790.0, 1000.0, 3790.0),
            )
            _add_raw_import(
                conn,
                source="jquants_daily_bars",
                record_count=1,
                min_date="2024-03-19",
                max_date="2024-04-18",
            )
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            bars = provider.get_eq_bars_daily_range(date(2024, 3, 19), date(2024, 4, 18))

            self.assertEqual(client.bars_calls, [])
            self.assertEqual(len(bars), 1)
            self.assertEqual(bars[0].close, 3790.0)

    def test_get_bars_falls_back_when_range_not_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            _add_raw_import(
                conn,
                source="jquants_daily_bars",
                record_count=0,
                min_date="2024-04-01",
                max_date="2024-04-10",
            )
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            # Range starts before imported window, so fall through to API/JSON.
            provider.get_eq_bars_daily_range(date(2024, 3, 19), date(2024, 4, 5))

            self.assertGreater(len(client.bars_calls), 0)

    def test_cache_only_raises_when_bars_range_not_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            _add_raw_import(
                conn,
                source="jquants_daily_bars",
                record_count=0,
                min_date="2024-04-01",
                max_date="2024-04-10",
            )
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider(
                "token",
                cache_dir,
                client=client,
                sqlite_path=sqlite_path,
                cache_only=True,
            )

            with self.assertRaisesRegex(JQuantsProviderError, "SQLite cache incomplete"):
                provider.get_eq_bars_daily_range(date(2024, 3, 19), date(2024, 4, 5))

            self.assertEqual(client.bars_calls, [])

    def test_get_bars_falls_back_when_imported_chunks_have_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            conn = open_connection(sqlite_path)
            for traded_at, close in (("2024-03-19", 3790.0), ("2024-03-25", 3810.0)):
                conn.execute(
                    "INSERT INTO jquants_daily_bars("
                    "ticker, traded_at, close, turnover_value, adjustment_close"
                    ") VALUES (?, ?, ?, ?, ?)",
                    ("1301", traded_at, close, 1000.0, close),
                )
            _add_raw_import(
                conn,
                source="jquants_daily_bars",
                record_count=1,
                min_date="2024-03-19",
                max_date="2024-03-20",
                path=(
                    "records/_data/raw/screening/jquants/"
                    "get_eq_bars_daily_range-end_dt-2024-03-20-start_dt-2024-03-19.json"
                ),
            )
            _add_raw_import(
                conn,
                source="jquants_daily_bars",
                record_count=1,
                min_date="2024-03-25",
                max_date="2024-04-18",
                path=(
                    "records/_data/raw/screening/jquants/"
                    "get_eq_bars_daily_range-end_dt-2024-04-18-start_dt-2024-03-25.json"
                ),
            )
            conn.commit()
            conn.close()

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            provider.get_eq_bars_daily_range(date(2024, 3, 19), date(2024, 4, 18))

            self.assertGreater(len(client.bars_calls), 0)

    def test_get_bars_fetches_only_missing_sqlite_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "raw"
            sqlite_path = Path(tmp) / "cache" / "market.sqlite"
            store_jquants_daily_bars(
                sqlite_path,
                [{"Code": "13010", "Date": "2024-03-19", "C": 3790.0, "Va": 1000.0}],
                requested_start=date(2024, 3, 19),
                requested_end=date(2024, 4, 18),
            )

            client = _RecordingClient()
            provider = JQuantsProvider("token", cache_dir, client=client, sqlite_path=sqlite_path)

            bars = provider.get_eq_bars_daily_range(date(2024, 3, 19), date(2024, 5, 20))

            self.assertNotIn(("2024-03-19", "2024-04-18"), client.bars_calls)
            self.assertIn(("2024-04-19", "2024-05-19"), client.bars_calls)
            self.assertIn(("2024-05-20", "2024-05-20"), client.bars_calls)
            self.assertGreaterEqual(len(bars), 3)

    def test_provider_works_when_sqlite_path_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _RecordingClient()
            provider = JQuantsProvider("token", Path(tmp), client=client)

            masters = provider.get_eq_master()

            self.assertEqual(client.eq_master_calls, 1)
            self.assertEqual(masters[0].name, "API_FALLBACK_NAME")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
