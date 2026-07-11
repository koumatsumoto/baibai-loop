from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.calibration.forward import ForwardReturnRow
from baibai_loop.screening.calibration.panel import build_panel
from baibai_loop.screening.calibration.store import (
    CalibrationCacheError,
    read_forward,
    read_panel,
    write_forward,
    write_panel,
)
from baibai_loop.screening.rule_config import load_screening_rules
from baibai_loop.screening.sqlite_cache import open_connection
from tests.helpers.screening_sqlite import add_source_coverage, insert_daily_bars_from_closes

ASOF = date(2026, 6, 30)


def _build_fixture_sqlite(sqlite_path: Path) -> None:
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_master_snapshots("
            "snapshot_date, ticker, name, market, sector_33, is_common_stock"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("2026-06-01", "9001", "キャッシュリッチ", "プライム", "サービス業", 1),
                ("2026-06-01", "9002", "割高", "プライム", "サービス業", 1),
            ],
        )
        add_source_coverage(
            conn,
            source="jquants_master_snapshots",
            coverage_key="latest",
            record_count=2,
            min_date="2026-06-01",
            max_date="2026-06-01",
        )
        fin_columns = (
            "ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding, "
            "sales, cfo, cash_eq, total_assets, equity, operating_profit, ordinary_profit, "
            "profit, fiscal_period, fiscal_year_end, period_start, period_end, "
            "dps_actual_annual, dps_forecast_annual"
        )
        conn.executemany(
            f"INSERT OR REPLACE INTO jquants_fin_summaries({fin_columns}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "9001",
                    "2026-05-10",
                    11.0,
                    10.0,
                    200.0,
                    1e8,
                    5e9,
                    1e9,
                    4e9,
                    1.5e10,
                    1e10,
                    5e8,
                    5e8,
                    4e8,
                    "FY",
                    "2026-03-31",
                    "2025-04-01",
                    "2026-03-31",
                    4.0,
                    4.5,
                ),
                (
                    "9002",
                    "2026-05-10",
                    1.0,
                    1.0,
                    10.0,
                    1e8,
                    5e9,
                    1e8,
                    1e8,
                    2e10,
                    5e9,
                    5e8,
                    5e8,
                    4e8,
                    "FY",
                    "2026-03-31",
                    "2025-04-01",
                    "2026-03-31",
                    None,
                    None,
                ),
            ],
        )
        add_source_coverage(
            conn,
            source="jquants_fin_summaries",
            coverage_key="2026",
            record_count=2,
            min_date="2026-05-10",
            max_date="2026-06-30",
        )
        conn.commit()
    finally:
        conn.close()
    for ticker in ("9001", "9002"):
        insert_daily_bars_from_closes(
            sqlite_path,
            ticker,
            [100.0] * 200,
            end_date=ASOF,
            turnover_value=2e8,
        )


class CalibrationPanelTest(unittest.TestCase):
    def test_build_panel_replays_screen_and_selection_point_in_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            rules = load_screening_rules()
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=rules)

            rows_by_ticker = {row.ticker: row for row in result.rows}
            self.assertEqual(set(rows_by_ticker), {"9001", "9002"})

            cheap = rows_by_ticker["9001"]
            self.assertTrue(cheap.in_population)
            self.assertTrue(cheap.pass_screen)
            self.assertEqual(cheap.evidence_playbooks, "cash-rich-asset-discount")
            self.assertEqual(cheap.selection_rank, 1)
            self.assertEqual(cheap.recommended_rank, 1)
            assert cheap.per_trailing is not None
            self.assertAlmostEqual(cheap.per_trailing, 10.0)
            assert cheap.pbr is not None
            self.assertAlmostEqual(cheap.pbr, 0.5)
            assert cheap.cash_to_market_cap is not None
            self.assertAlmostEqual(cheap.cash_to_market_cap, 0.4)
            assert cheap.dividend_yield is not None
            self.assertAlmostEqual(cheap.dividend_yield, 0.04)

            self.assertIsNone(rows_by_ticker["9002"].dividend_yield)

            expensive = rows_by_ticker["9002"]
            self.assertTrue(expensive.in_population)
            self.assertFalse(expensive.pass_screen)
            self.assertEqual(expensive.selection_rank, 2)
            self.assertEqual(expensive.recommended_rank, 2)

            diagnostics = result.diagnostics
            self.assertEqual(diagnostics.universe_size, 2)
            self.assertEqual(diagnostics.population_size, 2)
            self.assertEqual(diagnostics.candidates, 2)
            self.assertEqual(diagnostics.evidence_candidates, 1)
            self.assertEqual(diagnostics.population_per_trailing_nonnull, 2)

    def test_panel_and_forward_store_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            rules = load_screening_rules()
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=rules)

            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)
            loaded = read_panel(store_dir, ASOF)
            self.assertEqual(list(result.rows), loaded)

            forward_rows = [
                ForwardReturnRow(
                    asof=ASOF.isoformat(),
                    ticker="9001",
                    horizon="6m",
                    target_date="2026-12-29",
                    resolved=False,
                    price_return=None,
                    stale_price=False,
                    entry_date=ASOF.isoformat(),
                    exit_date=None,
                )
            ]
            write_forward(store_dir, ASOF, forward_rows)
            self.assertEqual(read_forward(store_dir, ASOF), forward_rows)

    def test_store_rejects_unversioned_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "calibration"
            store_dir.mkdir()
            (store_dir / f"panel-{ASOF.isoformat()}.csv").write_text("asof\n", encoding="utf-8")
            with self.assertRaisesRegex(CalibrationCacheError, "calibration-build --force"):
                read_panel(store_dir, ASOF)

    def test_store_rejects_partial_versioned_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)
            with self.assertRaisesRegex(CalibrationCacheError, "calibration-build --force"):
                read_forward(store_dir, ASOF)

    def test_missing_master_snapshot_becomes_unresolved_panel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            self.assertEqual(result.rows, ())
            self.assertEqual(result.diagnostics.master_snapshot_status, "unavailable")


if __name__ == "__main__":
    unittest.main()
