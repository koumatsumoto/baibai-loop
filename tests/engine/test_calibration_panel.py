from __future__ import annotations

import contextlib
import csv
import io
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tests.helpers.screening_sqlite import add_source_coverage, insert_daily_bars_from_closes

from baibai_engine.screening.calibration.cli import (
    calibration_build_command,
    calibration_evaluate_command,
)
from baibai_engine.screening.calibration.forward import (
    STALE_PRICE_MAX_LAG_DAYS,
    ForwardReturnRow,
)
from baibai_engine.screening.calibration.panel import (
    PRE2019_SELF_RANGE_POLICY,
    build_panel,
    rules_content_hash,
)
from baibai_engine.screening.calibration.store import (
    DEFAULT_CALIBRATION_DIR,
    CalibrationCacheError,
    read_forward,
    read_panel,
    write_forward,
    write_panel,
)
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.sqlite_cache import open_connection
from baibai_engine.screening.store_readiness import unreadable_store_reason

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
            "dps_actual_annual, dps_forecast_annual, treasury_shares, equity_to_asset_ratio"
        )
        conn.executemany(
            f"INSERT OR REPLACE INTO jquants_fin_summaries({fin_columns}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    0.0,
                    1e10 / 1.5e10,
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
                    0.0,
                    5e9 / 2e10,
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
        conn.execute(
            "INSERT INTO edinet_metrics("
            "asof_date, ticker, debt, cash, net_cash, investment_securities, "
            "failure_reasons, extractor_revision"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ASOF.isoformat(),
                "9001",
                1e9,
                4e9,
                3e9,
                2e9,
                "[]",
                "a" * 64,
            ),
        )
        add_source_coverage(
            conn,
            source="edinet_metrics",
            coverage_key=ASOF.isoformat(),
            record_count=1,
            min_date=ASOF.isoformat(),
            max_date=ASOF.isoformat(),
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
            self.assertEqual(cheap.investment_securities, 2e9)
            self.assertAlmostEqual(cheap.asset_backed_ratio or 0.0, 0.5)
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

    def test_panel_reads_three_fy_return_history_without_widening_metric_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            conn = open_connection(sqlite_path)
            try:
                conn.executemany(
                    "INSERT INTO jquants_fin_summaries("
                    "ticker, disclosed_at, shares_outstanding, fiscal_period, fiscal_year_end, "
                    "period_start, period_end, dps_actual_annual, dps_forecast_annual"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            "9001",
                            "2024-05-10",
                            102_000_000.0,
                            "FY",
                            "2024-03-31",
                            "2023-04-01",
                            "2024-03-31",
                            3.0,
                            3.0,
                        ),
                        (
                            "9001",
                            "2025-05-10",
                            101_000_000.0,
                            "FY",
                            "2025-03-31",
                            "2024-04-01",
                            "2025-03-31",
                            3.5,
                            3.5,
                        ),
                    ],
                )
                add_source_coverage(
                    conn,
                    source="jquants_fin_summaries",
                    coverage_key="return-history",
                    record_count=4,
                    min_date="2024-05-10",
                    max_date=ASOF.isoformat(),
                )
                conn.commit()
            finally:
                conn.close()
            for ticker in ("9001", "9002"):
                insert_daily_bars_from_closes(
                    sqlite_path,
                    ticker,
                    [100.0] * 800,
                    end_date=ASOF,
                    turnover_value=2e8,
                )

            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            row = {item.ticker: item for item in result.rows}["9001"]

            self.assertTrue(row.dps_streak_up)
            self.assertAlmostEqual(row.dps_yoy_latest or 0.0, (4.0 / 3.5) - 1.0)
            self.assertTrue(row.dps_guidance_up)
            self.assertEqual(row.share_count_reduction_streak, 2)
            self.assertTrue(row.shareholder_return_change)
            self.assertEqual(
                result.diagnostics.effective_fin_start,
                (ASOF - timedelta(days=730)).isoformat(),
            )

    def test_panel_normalizes_old_fy_eps_for_split_before_recent_bar_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            old_history_start = date(2022, 5, 10)
            insert_daily_bars_from_closes(
                sqlite_path,
                "9001",
                [100.0] * ((ASOF - old_history_start).days + 1),
                end_date=ASOF,
                turnover_value=2e8,
            )
            conn = open_connection(sqlite_path)
            try:
                conn.executemany(
                    "INSERT INTO jquants_fin_summaries("
                    "ticker, disclosed_at, eps_ttm, fiscal_period, fiscal_year_end, "
                    "period_start, period_end"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            "9001",
                            f"{year}-05-10",
                            eps,
                            "FY",
                            f"{year}-03-31",
                            f"{year - 1}-04-01",
                            f"{year}-03-31",
                        )
                        for year, eps in ((2022, 40.0), (2023, 20.0), (2024, 20.0), (2025, 20.0))
                    ],
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO jquants_daily_bars("
                    "ticker, traded_at, close, adjustment_close, adjustment_factor"
                    ") VALUES (?, ?, ?, ?, ?)",
                    [
                        ("9001", "2022-05-10", 50.0, 50.0, 1.0),
                        ("9001", "2022-10-03", 50.0, 100.0, 0.5),
                    ],
                )
                add_source_coverage(
                    conn,
                    source="jquants_fin_summaries",
                    coverage_key="normalized-profit-history",
                    record_count=6,
                    min_date=old_history_start.isoformat(),
                    max_date=ASOF.isoformat(),
                )
                conn.commit()
            finally:
                conn.close()

            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            row = {item.ticker: item for item in result.rows}["9001"]

            # 2022 FY EPS 40 is adjusted to 20 by a split outside the recent bar window.
            self.assertAlmostEqual(row.normalized_per_5fy or 0.0, 100.0 / 18.0)

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
            self.assertEqual(result.rows[0].realized_volatility_60d, 0.0)

            forward_rows = [
                ForwardReturnRow(
                    asof=ASOF.isoformat(),
                    ticker="9001",
                    horizon="6m",
                    target_date="2026-12-29",
                    resolved=True,
                    price_return=0.10,
                    stale_price=False,
                    entry_date=ASOF.isoformat(),
                    exit_date="2026-12-29",
                    realized_dividend_sum=12.5,
                    realized_dividend_fy_count=1,
                    total_return=0.125,
                    total_return_status="resolved",
                )
            ]
            write_forward(store_dir, ASOF, forward_rows)
            self.assertEqual(read_forward(store_dir, ASOF), forward_rows)

    def test_store_accepts_ratio_built_from_exact_market_cap_behind_rounded_oku(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            rounded_row = replace(
                result.rows[0],
                market_cap_oku=528.0,
                net_cash_to_market_cap=-1.156717974570965,
                investment_securities=21_269_000_000.0,
                asset_backed_ratio=-0.7537593706932526,
            )
            store_dir = Path(tmp) / "calibration"

            write_panel(store_dir, ASOF, (rounded_row,), result.diagnostics)

            self.assertEqual(read_panel(store_dir, ASOF), [rounded_row])

    def test_store_accepts_non_population_ratio_without_liquidity_market_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            excluded_row = replace(
                result.rows[0],
                in_population=False,
                market_cap_oku=None,
                net_cash_to_market_cap=-0.6,
                investment_securities=2_000_000_000.0,
                asset_backed_ratio=-0.4,
            )
            store_dir = Path(tmp) / "calibration"

            write_panel(store_dir, ASOF, (excluded_row,), result.diagnostics)

            self.assertEqual(read_panel(store_dir, ASOF), [excluded_row])

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

    def test_store_rejects_total_return_basis_or_status_bypass(self) -> None:
        for field, invalid in (
            ("total_return_basis", "price_return_only"),
            ("total_return_status", "resolved_by_claim"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                sqlite_path = Path(tmp) / "market.sqlite"
                _build_fixture_sqlite(sqlite_path)
                result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
                store_dir = Path(tmp) / "calibration"
                write_panel(store_dir, ASOF, result.rows, result.diagnostics)
                row = ForwardReturnRow(
                    asof=ASOF.isoformat(),
                    ticker="9001",
                    horizon="1y",
                    target_date="2027-06-30",
                    resolved=True,
                    price_return=0.10,
                    stale_price=False,
                    entry_date=ASOF.isoformat(),
                    exit_date="2027-06-30",
                    realized_dividend_sum=10.0,
                    realized_dividend_fy_count=1,
                    total_return=0.12,
                    total_return_status="resolved",
                )
                write_forward(store_dir, ASOF, [row])
                path = store_dir / f"forward-{ASOF.isoformat()}.csv"
                with path.open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                    fieldnames = list(rows[0])
                rows[0][field] = invalid
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)

                with self.assertRaisesRegex(CalibrationCacheError, "forward cache is invalid"):
                    read_forward(store_dir, ASOF)

    def test_store_rejects_an_unknown_population_coverage_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)
            path = store_dir / f"panel-{ASOF.isoformat()}.csv"
            with path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
                fieldnames = list(rows[0])
            rows[0]["population_coverage_status"] = "unknown"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            with self.assertRaisesRegex(CalibrationCacheError, "cache is invalid"):
                read_panel(store_dir, ASOF)

    def test_store_rejects_invalid_shareholder_return_change_fields(self) -> None:
        for field, invalid in (
            ("dps_guidance_up", "claimed_true"),
            ("dps_yoy_latest", "nan"),
            ("share_count_reduction_streak", "3"),
            ("shareholder_return_change", "false"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                sqlite_path = Path(tmp) / "market.sqlite"
                _build_fixture_sqlite(sqlite_path)
                result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
                store_dir = Path(tmp) / "calibration"
                write_panel(store_dir, ASOF, result.rows, result.diagnostics)
                path = store_dir / f"panel-{ASOF.isoformat()}.csv"
                with path.open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                    fieldnames = list(rows[0])
                rows[0][field] = invalid
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)

                with self.assertRaisesRegex(CalibrationCacheError, "cache is invalid"):
                    read_panel(store_dir, ASOF)

    def test_store_rejects_invalid_asset_backed_fields(self) -> None:
        invalid_updates = (
            {"investment_securities": "-1"},
            {"asset_backed_ratio": "nan"},
            {"asset_backed_ratio": "0.6"},
        )
        for updates in invalid_updates:
            with self.subTest(updates=updates), tempfile.TemporaryDirectory() as tmp:
                sqlite_path = Path(tmp) / "market.sqlite"
                _build_fixture_sqlite(sqlite_path)
                result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
                store_dir = Path(tmp) / "calibration"
                write_panel(store_dir, ASOF, result.rows, result.diagnostics)
                path = store_dir / f"panel-{ASOF.isoformat()}.csv"
                with path.open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                    fieldnames = list(rows[0])
                rows[0].update(updates)
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)

                with self.assertRaisesRegex(CalibrationCacheError, "cache is invalid"):
                    read_panel(store_dir, ASOF)

    def test_store_rejects_invalid_margin_hypothesis_fields(self) -> None:
        for field, invalid in (
            ("margin_short_to_adv", "-0.1"),
            ("realized_volatility_60d", "-0.1"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                sqlite_path = Path(tmp) / "market.sqlite"
                _build_fixture_sqlite(sqlite_path)
                result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
                store_dir = Path(tmp) / "calibration"
                write_panel(store_dir, ASOF, result.rows, result.diagnostics)
                path = store_dir / f"panel-{ASOF.isoformat()}.csv"
                with path.open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                    fieldnames = list(rows[0])
                rows[0][field] = invalid
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)

                with self.assertRaisesRegex(CalibrationCacheError, "cache is invalid"):
                    read_panel(store_dir, ASOF)

    def test_store_rejects_invalid_normalized_profit_fields(self) -> None:
        invalid_updates = (
            {"normalized_per_3fy": "-1"},
            {"self_range_observed_sessions": "-1"},
        )
        for updates in invalid_updates:
            with self.subTest(updates=updates), tempfile.TemporaryDirectory() as tmp:
                sqlite_path = Path(tmp) / "market.sqlite"
                _build_fixture_sqlite(sqlite_path)
                result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
                store_dir = Path(tmp) / "calibration"
                write_panel(store_dir, ASOF, result.rows, result.diagnostics)
                path = store_dir / f"panel-{ASOF.isoformat()}.csv"
                with path.open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                    fieldnames = list(rows[0])
                rows[0].update(updates)
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)

                with self.assertRaisesRegex(CalibrationCacheError, "cache is invalid"):
                    read_panel(store_dir, ASOF)

    def test_store_rejects_unversioned_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "calibration"
            store_dir.mkdir()
            (store_dir / f"panel-{ASOF.isoformat()}.csv").write_text("asof\n", encoding="utf-8")
            with self.assertRaisesRegex(CalibrationCacheError, "calibration-build --force"):
                read_panel(store_dir, ASOF)

    def test_store_rejects_previous_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)
            (store_dir / "calibration.meta.yaml").write_text(
                "cache_schema_version: 6\n", encoding="utf-8"
            )

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

    def test_panel_marks_priced_master_member_that_cannot_enter_the_universe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            conn = open_connection(sqlite_path)
            try:
                conn.execute(
                    "INSERT INTO jquants_master_snapshots("
                    "snapshot_date, ticker, name, market, sector_33, is_common_stock"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    ("2026-06-01", "9003", "履歴不足", "プライム", "サービス業", 1),
                )
                conn.execute(
                    "UPDATE source_coverage SET record_count = 3 "
                    "WHERE source = 'jquants_master_snapshots'"
                )
                conn.commit()
            finally:
                conn.close()
            insert_daily_bars_from_closes(
                sqlite_path, "9003", [100.0], end_date=ASOF, turnover_value=2e8
            )

            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            row = next(row for row in result.rows if row.ticker == "9003")

            self.assertEqual(row.population_coverage_status, "priced_master_without_universe")
            self.assertEqual(result.diagnostics.priced_master_without_universe_count, 1)

    def test_pre2019_variant_is_degraded_and_has_distinct_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            rules = load_screening_rules()

            result = build_panel(
                ASOF,
                sqlite_path=sqlite_path,
                rules=rules,
                policy=PRE2019_SELF_RANGE_POLICY,
            )

            self.assertFalse(result.diagnostics.production_authority)
            self.assertEqual(result.diagnostics.panel_variant, "pre2019_self_range_375")
            self.assertEqual(result.diagnostics.self_range_history_sessions, 375)
            self.assertEqual(result.diagnostics.bars_input_window_days, 600)
            self.assertTrue(all(row.self_range_degraded for row in result.rows))
            self.assertNotEqual(
                result.diagnostics.rules_hash,
                rules_content_hash(rules),
            )

    def test_pre2019_variant_cannot_use_the_production_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            errors = io.StringIO()

            with contextlib.redirect_stderr(errors):
                code = calibration_build_command(
                    sqlite_path=sqlite_path,
                    calibration_dir=DEFAULT_CALIBRATION_DIR,
                    rules=load_screening_rules(),
                    start=ASOF,
                    end=ASOF,
                    panel_variant="pre2019_self_range_375",
                )

            self.assertEqual(code, 1)
            self.assertIn("separate --calibration-dir", errors.getvalue())

    def test_pre2019_variant_is_rejected_for_production_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(
                ASOF,
                sqlite_path=sqlite_path,
                rules=load_screening_rules(),
                policy=PRE2019_SELF_RANGE_POLICY,
            )
            store_dir = Path(tmp) / "calibration-pre2019"
            write_panel(
                store_dir,
                ASOF,
                result.rows,
                replace(result.diagnostics, production_authority=True),
            )
            write_forward(store_dir, ASOF, [])
            errors = io.StringIO()

            with contextlib.redirect_stderr(errors):
                code = calibration_evaluate_command(
                    calibration_dir=store_dir,
                    horizons=["3y", "5y"],
                    run_purpose="production_decision",
                    required_asofs=[ASOF.isoformat()],
                    required_metrics=[
                        "recommended_rank_top5",
                        "recommended_rank_top10",
                        "er_calibration",
                    ],
                )

            self.assertEqual(code, 1)
            self.assertIn("no production authority", errors.getvalue())

    def test_degraded_row_is_rejected_even_with_production_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            degraded_rows = (replace(result.rows[0], self_range_degraded=True), *result.rows[1:])
            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, degraded_rows, result.diagnostics)
            write_forward(store_dir, ASOF, [])
            errors = io.StringIO()

            with contextlib.redirect_stderr(errors):
                code = calibration_evaluate_command(
                    calibration_dir=store_dir,
                    horizons=["3y", "5y"],
                    run_purpose="production_decision",
                    required_asofs=[ASOF.isoformat()],
                    required_metrics=[
                        "recommended_rank_top5",
                        "recommended_rank_top10",
                        "er_calibration",
                    ],
                )

            self.assertEqual(code, 1)
            self.assertIn("no production authority", errors.getvalue())

    def test_build_requires_force_when_existing_panel_contract_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            store_dir = Path(tmp) / "calibration-custom"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)
            errors = io.StringIO()

            with (
                contextlib.redirect_stderr(errors),
                patch(
                    "baibai_engine.screening.calibration.cli.month_end_asof_grid",
                    return_value=[ASOF],
                ),
            ):
                code = calibration_build_command(
                    sqlite_path=sqlite_path,
                    calibration_dir=store_dir,
                    rules=load_screening_rules(),
                    start=ASOF,
                    end=ASOF,
                    panel_variant="pre2019_self_range_375",
                )

            self.assertEqual(code, 1)
            self.assertIn("contract differs", errors.getvalue())

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
