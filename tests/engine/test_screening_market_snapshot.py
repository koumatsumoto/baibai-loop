from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.screening.cli import build_parser, market_snapshot_command
from baibai_engine.screening.market_snapshot import build_market_snapshot
from baibai_engine.screening.sqlite_cache import open_connection

_ASOF = date(2026, 5, 29)


def _insert_bars(sqlite_path: Path, ticker: str, closes: list[float], *, end: date) -> None:
    insert_daily_bars_from_closes(sqlite_path, ticker, closes, end_date=end)


def _insert_sector(sqlite_path: Path, ticker: str, sector: str) -> None:
    conn = open_connection(sqlite_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO jquants_master_snapshots"
            "(snapshot_date, ticker, name, market, sector_33, is_common_stock)"
            " VALUES ('2026-05-28', ?, ?, 'プライム', ?, 1)",
            (ticker, f"name-{ticker}", sector),
        )
        conn.commit()
    finally:
        conn.close()


class BuildMarketSnapshotTests(unittest.TestCase):
    def test_close_null_adjustment_event_is_applied_to_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", [1.0] * 50 + [100.0] * 10, end=_ASOF)
            conn = open_connection(sqlite_path)
            try:
                event_day = date(2026, 5, 20)
                conn.execute(
                    "INSERT OR REPLACE INTO jquants_daily_bars"
                    "(ticker, traded_at, close, adjustment_factor) VALUES (?, ?, NULL, 100.0)",
                    ("1321", event_day.isoformat()),
                )
                conn.commit()
            finally:
                conn.close()

            payload = build_market_snapshot(
                sqlite_path=sqlite_path,
                asof_date=_ASOF,
                history_weeks=1,
                min_breadth_sample=1,
            )

            points = payload["points"]
            assert isinstance(points, list)
            self.assertAlmostEqual(float(points[-1]["benchmark_return_20d"]), 0.0, places=12)

    def test_points_and_sectors_reflect_trend_and_breadth(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            # benchmark up ~0.3%/day (>3% per 20 bars -> rally), one rising and
            # one falling sector member.
            _insert_bars(sqlite_path, "1321", [100 * 1.003**i for i in range(120)], end=_ASOF)
            _insert_bars(sqlite_path, "AAAA", [50 * 1.005**i for i in range(120)], end=_ASOF)
            _insert_bars(sqlite_path, "BBBB", [80 * 0.997**i for i in range(120)], end=_ASOF)
            _insert_sector(sqlite_path, "AAAA", "機械")
            _insert_sector(sqlite_path, "BBBB", "小売業")

            payload = build_market_snapshot(
                sqlite_path=sqlite_path,
                asof_date=_ASOF,
                history_weeks=4,
                min_breadth_sample=2,
            )

            points = payload["points"]
            assert isinstance(points, list)
            self.assertEqual(len(points), 4)
            self.assertEqual(points[-1]["date"], _ASOF.isoformat())
            self.assertLess(points[0]["date"], points[-1]["date"])
            last = points[-1]
            assert isinstance(last["benchmark_return_20d"], float)
            self.assertGreater(last["benchmark_return_20d"], 0.03)
            self.assertEqual(last["benchmark_trend"], "uptrend")
            self.assertEqual(last["breadth_sample_size"], 3)
            assert isinstance(last["breadth_pct_above_ma20"], float)

            sectors = payload["sectors"]
            assert isinstance(sectors, list)
            self.assertEqual([row["sector_33"] for row in sectors], ["機械", "小売業"])
            rising = sectors[0]
            assert isinstance(rising["median_return_20d"], float)
            self.assertGreater(rising["median_return_20d"], 0)
            self.assertEqual(rising["pct_above_ma20"], 1.0)
            falling = sectors[1]
            assert isinstance(falling["median_return_20d"], float)
            self.assertLess(falling["median_return_20d"], 0)

    def test_breadth_degrades_below_min_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", [100.0] * 120, end=_ASOF)

            payload = build_market_snapshot(
                sqlite_path=sqlite_path,
                asof_date=_ASOF,
                history_weeks=1,
                min_breadth_sample=5,
            )
            points = payload["points"]
            assert isinstance(points, list)
            self.assertIsNone(points[0]["breadth_pct_above_ma20"])
            self.assertEqual(points[0]["benchmark_trend"], "neutral")

    def test_sectors_do_not_restore_tickers_from_older_master_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "OLD1", [100 * 1.01**i for i in range(30)], end=_ASOF)
            _insert_bars(sqlite_path, "LIVE", [100 * 1.01**i for i in range(30)], end=_ASOF)
            conn = open_connection(sqlite_path)
            try:
                conn.executemany(
                    "INSERT INTO jquants_master_snapshots"
                    "(snapshot_date, ticker, name, market, sector_33, is_common_stock)"
                    " VALUES (?, ?, ?, 'プライム', ?, 1)",
                    (
                        ("2026-05-28", "OLD1", "旧構成銘柄", "旧構成セクター"),
                        ("2026-05-29", "LIVE", "現構成銘柄", "機械"),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            payload = build_market_snapshot(
                sqlite_path=sqlite_path,
                asof_date=_ASOF,
                history_weeks=1,
                min_breadth_sample=1,
            )

            sectors = payload["sectors"]
            assert isinstance(sectors, list)
            self.assertEqual([row["sector_33"] for row in sectors], ["機械"])


class MarketSnapshotCliTests(unittest.TestCase):
    def test_parser_accepts_market_snapshot_arguments(self) -> None:
        args = build_parser().parse_args(
            ["market-snapshot", "--asof", "2026-05-29", "--weeks", "8"]
        )
        self.assertEqual(args.weeks, 8)

    def test_command_emits_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", [100.0] * 120, end=_ASOF)
            buffer = io.StringIO()
            exit_code = market_snapshot_command(
                asof=_ASOF.isoformat(),
                weeks=2,
                sqlite_path=sqlite_path,
                stdout=buffer,
            )
            self.assertEqual(exit_code, 0)
            payload = safe_load(buffer.getvalue())
            self.assertEqual(payload["as_of"], _ASOF.isoformat())
            self.assertEqual(len(payload["points"]), 2)

    def test_command_emits_one_json_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", [100.0] * 120, end=_ASOF)
            buffer = io.StringIO()
            exit_code = market_snapshot_command(
                asof=_ASOF.isoformat(),
                weeks=2,
                sqlite_path=sqlite_path,
                stdout=buffer,
                output_format="json",
            )
            self.assertEqual(exit_code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["as_of"], _ASOF.isoformat())

    def test_command_rejects_non_positive_weeks(self) -> None:
        exit_code = market_snapshot_command(
            asof=_ASOF.isoformat(),
            weeks=0,
            sqlite_path=Path("/nonexistent.sqlite"),
            stdout=io.StringIO(),
        )
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
