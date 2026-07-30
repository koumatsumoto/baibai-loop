from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.calibration.cli import calibration_build_command
from baibai_engine.screening.calibration.forward import (
    STALE_PRICE_MAX_LAG_DAYS,
    ForwardReturnRow,
)
from baibai_engine.screening.calibration.panel import build_panel
from baibai_engine.screening.calibration.store import (
    CalibrationCacheError,
    read_forward,
    read_panel,
    write_forward,
    write_panel,
)
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.sqlite_cache import open_connection
from baibai_engine.screening.store_readiness import unreadable_store_reason
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
            # carry 用配当利回りは予想 DPS (4.5) を実績 (4.0) より優先する。
            assert cheap.dividend_yield is not None
            self.assertAlmostEqual(cheap.dividend_yield, 0.045)

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

    def test_store_reads_a_cache_that_carries_a_column_the_contract_dropped(self) -> None:
        # 46 cohort を読み続けられることが、判定を評価時導出にした前提そのもの。
        # 厳格一致へ戻すと既存 store が読めなくなるので、その契約を固定する。
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)
            rows = [
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
            write_forward(store_dir, ASOF, rows)
            path = store_dir / f"forward-{ASOF.isoformat()}.csv"
            header, body = path.read_text(encoding="utf-8").splitlines()
            path.write_text(
                f"{header},retired_column\n{body},not_assessed\n",
                encoding="utf-8",
            )

            self.assertEqual(read_forward(store_dir, ASOF), rows)

    def test_store_rejects_a_cache_missing_a_column_the_contract_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)
            write_forward(store_dir, ASOF, [])
            path = store_dir / f"forward-{ASOF.isoformat()}.csv"
            header = path.read_text(encoding="utf-8").splitlines()[0]
            kept = [name for name in header.split(",") if name != "status"]
            path.write_text(",".join(kept) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(CalibrationCacheError, "missing status"):
                read_forward(store_dir, ASOF)

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

    def test_panel_counts_asof_priced_tickers_the_master_read_omits(self) -> None:
        # asof 当日に価格がありながら master に居ない銘柄は、その断面が投資可能
        # universe を再現していないことの証拠なので数える。
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            insert_daily_bars_from_closes(
                sqlite_path, "9003", [100.0] * 200, end_date=ASOF, turnover_value=2e8
            )
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())

            diagnostics = result.diagnostics
            self.assertEqual(diagnostics.asof_priced_count, 3)
            self.assertEqual(diagnostics.asof_population_mismatch_count, 1)
            self.assertNotIn("9003", {row.ticker for row in result.rows})

    def test_panel_does_not_count_a_name_delisted_before_asof_as_a_population_hole(self) -> None:
        # asof 前に最終売買を終えた銘柄は as-of に投資可能でないので、master が
        # それを持たないのは正しい。entry の staleness 許容を population の定義へ
        # 流用すると、どの master でも mismatch を 0 にできなくなる。
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            insert_daily_bars_from_closes(
                sqlite_path,
                "9004",
                [100.0] * 200,
                end_date=ASOF - timedelta(days=5),
                turnover_value=2e8,
            )
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())

            diagnostics = result.diagnostics
            self.assertEqual(diagnostics.asof_priced_count, 2)
            self.assertEqual(diagnostics.asof_population_mismatch_count, 0)

    def test_panel_reports_no_mismatch_when_master_holds_every_asof_priced_ticker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())

            diagnostics = result.diagnostics
            self.assertEqual(diagnostics.asof_priced_count, 2)
            self.assertEqual(diagnostics.asof_population_mismatch_count, 0)
            self.assertEqual(diagnostics.priced_master_without_universe_count, 0)
            self.assertEqual(diagnostics.entry_resolution_lag_days, STALE_PRICE_MAX_LAG_DAYS)

    def test_missing_master_snapshot_becomes_unresolved_panel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            self.assertEqual(result.rows, ())
            self.assertEqual(result.diagnostics.master_snapshot_status, "unavailable")

    def test_build_refuses_a_store_behind_the_current_schema(self) -> None:
        # Reading such a store degrades to "nothing here", which a build would write
        # out as a grid of empty cohorts and count as built. The operator has to be
        # told the store is stale instead of receiving panels holding nobody.
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            conn = open_connection(sqlite_path)
            try:
                conn.execute("PRAGMA user_version = 1")
                conn.commit()
            finally:
                conn.close()
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = calibration_build_command(
                    sqlite_path=sqlite_path,
                    calibration_dir=Path(tmp) / "calibration",
                    rules=load_screening_rules(),
                    start=ASOF,
                    end=ASOF,
                )

            self.assertEqual(code, 1)
            self.assertIn("user_version 1", errors.getvalue())
            self.assertFalse((Path(tmp) / "calibration").exists())

    def test_a_store_at_the_current_schema_is_not_refused(self) -> None:
        # The refusal above is only correct if it lets a healthy store through; a guard
        # that blocked one would stop every measurement with the same message.
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)

            self.assertIsNone(unreadable_store_reason(sqlite_path))

    def test_a_missing_store_is_named_as_missing_rather_than_stale(self) -> None:
        # The two send the operator to different places, and only one of them is a
        # multi-hour re-fetch.
        with tempfile.TemporaryDirectory() as tmp:
            reason = unreadable_store_reason(Path(tmp) / "absent.sqlite")

            assert reason is not None
            self.assertIn("not found", reason)
            self.assertFalse((Path(tmp) / "absent.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
