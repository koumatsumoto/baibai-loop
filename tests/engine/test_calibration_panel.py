from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tests.helpers.calibration_store import publish_panel, synthetic_calibration_source
from tests.helpers.screening_sqlite import add_source_coverage, insert_daily_bars_from_closes

from baibai_engine.market.lake.keys import current_calibration_bundle_pointer_key
from baibai_engine.screening.calibration.cli import (
    calibration_build_command,
    calibration_evaluate_command,
)
from baibai_engine.screening.calibration.forward import (
    STALE_PRICE_MAX_LAG_DAYS,
    TOTAL_RETURN_BASIS,
    ForwardReturnRow,
)
from baibai_engine.screening.calibration.identity import rules_contract_hash
from baibai_engine.screening.calibration.lake import (
    CALIBRATION_FORWARD,
    CALIBRATION_PANEL,
    CalibrationLakeError,
    require_build_inputs,
)
from baibai_engine.screening.calibration.panel import (
    PRE2019_SELF_RANGE_POLICY,
    build_panel,
    rules_content_hash,
)
from baibai_engine.screening.calibration.store import (
    CACHE_SCHEMA_VERSIONS,
    DEFAULT_CALIBRATION_DIR,
    CalibrationCacheError,
    forward_row_from_mapping,
    panel_row_from_mapping,
    published_cohorts,
    read_forward,
    read_panel,
    read_panel_meta,
    resolve_calibration_bundle,
)
from baibai_engine.screening.calibration.store import (
    write_forward as _write_forward,
)
from baibai_engine.screening.calibration.store import (
    write_panel as _write_panel,
)
from baibai_engine.screening.metrics import (
    BARS_INPUT_WINDOW_DAYS,
    VALUATION_HISTORY_SESSIONS,
    build_profitability_level_signals,
)
from baibai_engine.screening.providers.jquants import JQuantsFinancialSummary
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.sqlite_cache import open_connection
from baibai_engine.screening.sqlite_reader import ReportedShortMetric
from baibai_engine.screening.store_readiness import unreadable_store_reason

ASOF = date(2026, 6, 30)


def write_panel(root: Path, asof: date, *args: object, **kwargs: object) -> None:
    _write_panel(
        root,
        asof,
        *args,
        source=synthetic_calibration_source(captured_on=asof),
        input_cutoff=asof,
        **kwargs,
    )


def write_forward(root: Path, asof: date, *args: object, **kwargs: object) -> None:
    _write_forward(
        root,
        asof,
        *args,
        source=synthetic_calibration_source(captured_on=asof),
        input_cutoff=asof,
        **kwargs,
    )


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
                    3e10,
                    2e10,
                    5e8,
                    5e8,
                    # 報告純利益は 1 株当たり当期純利益 x 自己株控除後株数と一致する。
                    # 自己資本も `bps x 自己株控除後株数 == 総資産 x 自己資本比率`
                    # (200 x 1e8 == 3e10 x 2/3) を満たす。倍率はこの行から出るので、
                    # 行の中で両方の恒等式が成り立っている必要がある。
                    1e9,
                    "FY",
                    "2026-03-31",
                    "2025-04-01",
                    "2026-03-31",
                    4.0,
                    4.5,
                    0.0,
                    2e10 / 3e10,
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
                    1e8,
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
            # 総資産と基準は、EDINET の貸借対照表が短信と同じ実体を指すことを示す事実として
            # 持つ。短信の総資産 (3e10) と揃わない行は EDINET 由来の値を出さない。
            "INSERT INTO edinet_metrics("
            "asof_date, ticker, debt, cash, net_cash, investment_securities, "
            "total_assets, consolidation_basis, failure_reasons, extractor_revision"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ASOF.isoformat(),
                "9001",
                1e9,
                4e9,
                3e9,
                2e9,
                3e10,
                "consolidated",
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


def _current_panel_manifest(root):  # type: ignore[no-untyped-def]
    return resolve_calibration_bundle(root).datasets[CALIBRATION_PANEL.name]


class CalibrationPanelTest(unittest.TestCase):
    def setUp(self) -> None:
        identity = patch(
            "baibai_engine.screening.calibration.cli.verified_git_commit",
            return_value="a" * 40,
        )
        identity.start()
        self.addCleanup(identity.stop)
        store_identity = patch(
            "baibai_engine.screening.calibration.store.verified_git_commit",
            return_value="a" * 40,
        )
        store_identity.start()
        self.addCleanup(store_identity.stop)

    def test_a_filing_older_than_the_coverage_does_not_take_the_cohort_down(self) -> None:
        """The history floor follows what the store may serve, not its oldest row.

        Summaries arrive through a subscription window that moves, so a filing fetched
        while the window still reached that far back stays in the table after the window
        passes it. Reading from the oldest row then asks for a range the store refuses,
        and the cohort that only needs recent history dies over a filing from years ago.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            conn = open_connection(sqlite_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO jquants_fin_summaries("
                    "ticker, disclosed_at, eps_ttm, fiscal_period"
                    ") VALUES (?, ?, ?, ?)",
                    ("9001", "2016-08-01", 1.0, "FY"),
                )
                conn.commit()
            finally:
                conn.close()

            built = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())

            self.assertEqual({row.ticker for row in built.rows}, {"9001", "9002"})
            # The stranded filing stays unread: the panel keeps the multiples the covered
            # window produced instead of mixing in a row the store no longer stands behind.
            self.assertEqual(built.diagnostics.effective_fin_start, "2026-05-10")

    def test_policy_exclusions_count_every_reason_the_universe_counted(self) -> None:
        """One decision, read twice -- not made twice.

        A name can miss the population on more than one policy ground at once, and the
        universe counts each. Re-deriving the reasons here once produced an exclusive
        chain, so the same diagnostic name meant "any of these" on one surface and
        "the first of these" on the other.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            conn = open_connection(sqlite_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO jquants_master_snapshots("
                    "snapshot_date, ticker, name, market, sector_33, is_common_stock"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    ("2026-06-01", "1306", "指数連動 ETF", "その他", "その他", 1),
                )
                conn.commit()
            finally:
                conn.close()

            built = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())

            counts = built.diagnostics.policy_exclusion_reason_counts or {}
            self.assertEqual(counts.get("sector_out_of_classification"), 1)
            self.assertEqual(counts.get("market_out_of_scope"), 1)
            # Data-shortage reasons stay on the other side of the pair.
            self.assertNotIn("insufficient_bar_history", counts)
            self.assertNotIn("1306", {row.ticker for row in built.rows})

    def test_the_panel_counts_how_many_names_carry_an_edinet_axis(self) -> None:
        """A cohort with no EDINET source replays a screen production does not run.

        `read_edinet_metrics` returns an empty mapping when the store has no rows for the
        as-of, and the axes it feeds simply come out null. Production carries the same
        axes for 53-64% of names, so a cohort at zero is measuring a different screen —
        which nothing states unless the count is reported.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)

            with_edinet = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            self.assertGreaterEqual(with_edinet.diagnostics.population_edinet_axis_nonnull, 1)

            conn = open_connection(sqlite_path)
            try:
                conn.execute("DELETE FROM edinet_metrics")
                conn.commit()
            finally:
                conn.close()

            without_edinet = build_panel(
                ASOF, sqlite_path=sqlite_path, rules=load_screening_rules()
            )
            self.assertEqual(without_edinet.diagnostics.population_edinet_axis_nonnull, 0)
            # The population itself is unchanged: only the EDINET-derived axes go absent.
            self.assertEqual(
                without_edinet.diagnostics.population_size,
                with_edinet.diagnostics.population_size,
            )

    def test_panel_distinguishes_covered_no_report_from_source_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            with patch(
                "baibai_engine.screening.calibration.panel.read_reported_short_metrics",
                return_value={
                    "9001": ReportedShortMetric(
                        ratio=0.012,
                        breadth=2,
                        latest_disclosed_at=date(2026, 6, 25),
                    )
                },
            ):
                covered = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            covered_by_ticker = {row.ticker: row for row in covered.rows}
            self.assertEqual(covered_by_ticker["9001"].reported_short_ratio, 0.012)
            self.assertEqual(covered_by_ticker["9001"].reported_short_breadth, 2)
            self.assertEqual(
                covered_by_ticker["9001"].reported_short_latest_disclosed_at,
                "2026-06-25",
            )
            self.assertEqual(covered_by_ticker["9002"].reported_short_ratio, 0.0)
            self.assertEqual(covered_by_ticker["9002"].reported_short_breadth, 0)
            self.assertEqual(
                covered_by_ticker["9002"].reported_short_latest_disclosed_at,
                ASOF.isoformat(),
            )

            with patch(
                "baibai_engine.screening.calibration.panel.read_reported_short_metrics",
                return_value=None,
            ):
                uncovered = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            for row in uncovered.rows:
                self.assertIsNone(row.reported_short_ratio)
                self.assertIsNone(row.reported_short_breadth)
                self.assertIsNone(row.reported_short_latest_disclosed_at)

    def test_profitability_levels_use_pit_ttm_without_profit_fallback(self) -> None:
        summaries = (
            JQuantsFinancialSummary(
                ticker="9001",
                disclosed_at=date(2024, 8, 1),
                fiscal_year_end=date(2025, 3, 31),
                period_start=date(2024, 4, 1),
                period_end=date(2024, 6, 30),
                operating_profit=20.0,
                sales=250.0,
            ),
            JQuantsFinancialSummary(
                ticker="9001",
                disclosed_at=date(2025, 5, 10),
                fiscal_year_end=date(2025, 3, 31),
                period_start=date(2024, 4, 1),
                period_end=date(2025, 3, 31),
                operating_profit=100.0,
                sales=1000.0,
                total_assets=1900.0,
            ),
            JQuantsFinancialSummary(
                ticker="9001",
                disclosed_at=date(2025, 8, 1),
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                period_end=date(2025, 6, 30),
                operating_profit=30.0,
                sales=280.0,
                total_assets=2000.0,
            ),
            JQuantsFinancialSummary(
                ticker="9001",
                disclosed_at=date(2025, 9, 1),
                fiscal_year_end=date(2026, 3, 31),
                period_start=date(2025, 4, 1),
                period_end=date(2025, 6, 30),
                operating_profit=9999.0,
                sales=9999.0,
                total_assets=1.0,
            ),
        )

        levels = build_profitability_level_signals(
            summaries, date(2025, 8, 31), load_screening_rules().ttm
        )

        self.assertAlmostEqual(levels.operating_profit_to_assets or 0.0, 110.0 / 2000.0)
        self.assertAlmostEqual(levels.operating_margin or 0.0, 110.0 / 1030.0)
        self.assertAlmostEqual(levels.asset_turnover or 0.0, 1030.0 / 2000.0)

    def test_panel_and_store_round_trip_profitability_levels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            by_ticker = {row.ticker: row for row in result.rows}

            self.assertAlmostEqual(by_ticker["9001"].operating_profit_to_assets or 0.0, 5e8 / 3e10)
            self.assertAlmostEqual(by_ticker["9001"].operating_margin or 0.0, 5e8 / 5e9)
            self.assertAlmostEqual(by_ticker["9001"].asset_turnover or 0.0, 5e9 / 3e10)

            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)
            restored = {row.ticker: row for row in read_panel(store_dir, ASOF)}
            self.assertEqual(
                restored["9001"].operating_profit_to_assets,
                by_ticker["9001"].operating_profit_to_assets,
            )
            self.assertEqual(restored["9001"].operating_margin, by_ticker["9001"].operating_margin)
            self.assertEqual(restored["9001"].asset_turnover, by_ticker["9001"].asset_turnover)

            negative_sales_row = replace(
                result.rows[0],
                operating_profit_to_assets=0.05,
                operating_margin=-0.10,
                asset_turnover=-0.50,
            )
            write_panel(store_dir, ASOF, (negative_sales_row,), result.diagnostics)
            self.assertEqual(read_panel(store_dir, ASOF), [negative_sales_row])

    def test_valuation_calculation_revision_is_part_of_method_identity(self) -> None:
        rules = load_screening_rules()
        previous_identity = rules_contract_hash(
            rules.model_dump_json(),
            valuation_calculation_revision="gross-shares-capital-v0",
            variant="production",
            valuation_history_sessions=VALUATION_HISTORY_SESSIONS,
            bars_input_window_days=BARS_INPUT_WINDOW_DAYS,
            production_authority=True,
        )

        self.assertNotEqual(rules_content_hash(rules), previous_identity)

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

    def test_panel_return_signal_uses_an_action_event_without_a_close(self) -> None:
        """売買停止日のactionを、見かけの株数減少として較正へ入れない。"""

        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            conn = open_connection(sqlite_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO jquants_master_snapshots("
                    "snapshot_date, ticker, name, market, sector_33, is_common_stock"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    ("2026-06-01", "9003", "併合銘柄", "スタンダード", "サービス業", 1),
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO jquants_fin_summaries("
                    "ticker, disclosed_at, shares_outstanding, treasury_shares, fiscal_period, "
                    "fiscal_year_end, period_start, period_end"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            "9003",
                            "2024-05-10",
                            100_000_000.0,
                            0.0,
                            "FY",
                            "2024-03-31",
                            "2023-04-01",
                            "2024-03-31",
                        ),
                        (
                            "9003",
                            "2025-05-10",
                            100_000_000.0,
                            0.0,
                            "FY",
                            "2025-03-31",
                            "2024-04-01",
                            "2025-03-31",
                        ),
                        (
                            "9003",
                            "2026-05-10",
                            1_000_000.0,
                            0.0,
                            "FY",
                            "2026-03-31",
                            "2025-04-01",
                            "2026-03-31",
                        ),
                    ],
                )
                add_source_coverage(
                    conn,
                    source="jquants_fin_summaries",
                    coverage_key="return-event-history",
                    record_count=5,
                    min_date="2024-05-10",
                    max_date=ASOF.isoformat(),
                )
                conn.commit()
            finally:
                conn.close()
            insert_daily_bars_from_closes(
                sqlite_path,
                "9003",
                [100.0] * 800,
                end_date=ASOF,
                turnover_value=2e8,
            )
            conn = open_connection(sqlite_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO jquants_daily_bars("
                    "ticker, traded_at, close, adjustment_factor"
                    ") VALUES (?, ?, ?, ?)",
                    ("9003", "2025-06-02", None, 100.0),
                )
                conn.commit()
            finally:
                conn.close()

            with_event = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            with_event_row = {item.ticker: item for item in with_event.rows}["9003"]

            conn = open_connection(sqlite_path)
            try:
                conn.execute(
                    "UPDATE jquants_daily_bars SET adjustment_factor = 1 "
                    "WHERE ticker = ? AND traded_at = ?",
                    ("9003", "2025-06-02"),
                )
                conn.commit()
            finally:
                conn.close()
            without_event = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            without_event_row = {item.ticker: item for item in without_event.rows}["9003"]

            self.assertEqual(with_event_row.share_count_reduction_streak, 0)
            self.assertFalse(with_event_row.shareholder_return_change)
            self.assertEqual(without_event_row.share_count_reduction_streak, 1)
            self.assertTrue(without_event_row.shareholder_return_change)

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

    def _panel_row(self, **updates: object) -> dict[str, object]:
        """A stored panel row as native values, with the named fields replaced.

        The invariants below are what the store enforces on every row it reads, so
        they are exercised at the materializer rather than by editing a published
        object — an edited object fails on its digest long before a value is parsed.
        """

        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
        payload = asdict(result.rows[0])
        payload.update(updates)
        return payload

    def test_store_rejects_a_row_whose_population_coverage_status_is_unknown(self) -> None:
        row = self._panel_row(population_coverage_status="unknown")

        with self.assertRaisesRegex(ValueError, "population coverage status"):
            panel_row_from_mapping(row)

    def test_store_rejects_invalid_shareholder_return_change_fields(self) -> None:
        for updates in (
            {"dps_yoy_latest": float("nan")},
            {"share_count_reduction_streak": 3},
            {"shareholder_return_change": False},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                panel_row_from_mapping(self._panel_row(**updates))

    def test_store_rejects_invalid_asset_backed_fields(self) -> None:
        for updates in (
            {"investment_securities": -1.0},
            {"asset_backed_ratio": float("nan")},
            {"asset_backed_ratio": 0.6},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                panel_row_from_mapping(self._panel_row(**updates))

    def test_store_rejects_invalid_margin_hypothesis_fields(self) -> None:
        for updates in (
            {"margin_short_to_adv": -0.1},
            {"realized_volatility_60d": -0.1},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                panel_row_from_mapping(self._panel_row(**updates))

    def test_store_rejects_invalid_normalized_profit_fields(self) -> None:
        for updates in (
            {"normalized_per_3fy": -1.0},
            {"self_range_observed_sessions": -1},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                panel_row_from_mapping(self._panel_row(**updates))

    def test_store_rejects_total_return_basis_or_status_bypass(self) -> None:
        for field, invalid in (
            ("total_return_basis", "price_return_only"),
            ("total_return_status", "resolved_by_claim"),
        ):
            with self.subTest(field=field):
                payload = {
                    "asof": ASOF.isoformat(),
                    "ticker": "9001",
                    "horizon": "1y",
                    "target_date": "2027-06-30",
                    "resolved": True,
                    "price_return": 0.10,
                    "stale_price": False,
                    "entry_date": ASOF.isoformat(),
                    "exit_date": "2027-06-30",
                    "status": "resolved",
                    "adjustment_factor_coverage": "unknown",
                    "realized_dividend_sum": 10.0,
                    "realized_dividend_fy_count": 1,
                    "total_return": 0.12,
                    "total_return_status": "resolved",
                    "total_return_basis": TOTAL_RETURN_BASIS,
                }
                payload[field] = invalid

                with self.assertRaises(ValueError):
                    forward_row_from_mapping(payload)

    def test_store_rejects_a_published_object_whose_bytes_changed(self) -> None:
        """Tampering is caught by the object digest, before any value is read."""

        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)
            objects = sorted((store_dir / "lake" / "l2").rglob("*.parquet"))
            self.assertTrue(objects)
            objects[0].write_bytes(objects[0].read_bytes() + b"tamper")

            with self.assertRaisesRegex(CalibrationCacheError, "cache is invalid"):
                read_panel(store_dir, ASOF)

    def test_store_rejects_a_build_produced_by_another_transform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)

            with self.assertRaisesRegex(CalibrationLakeError, "different transform"):
                require_build_inputs(
                    _current_panel_manifest(store_dir),
                    dataset=CALIBRATION_PANEL,
                    cache_schema_version="0" * 16,
                )

    def test_store_rejects_a_build_that_was_not_bound_to_the_expected_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)

            with self.assertRaisesRegex(CalibrationLakeError, "not built from"):
                require_build_inputs(
                    _current_panel_manifest(store_dir),
                    dataset=CALIBRATION_PANEL,
                    cache_schema_version=CACHE_SCHEMA_VERSIONS[CALIBRATION_PANEL.name],
                    source_release_id="release-that-was-not-used",
                )

    def test_store_rejects_unversioned_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "calibration"
            store_dir.mkdir()
            (store_dir / f"panel-{ASOF.isoformat()}.csv").write_text("asof\n", encoding="utf-8")
            with self.assertRaisesRegex(CalibrationCacheError, "calibration-build --force"):
                read_panel(store_dir, ASOF)

    def test_store_rejects_a_panel_written_under_another_panel_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)
            # The contract a build was written under travels in its own transform
            # fingerprint, so a code change that moves the panel contract is what makes
            # the panel unreadable — there is no separate statement to rewrite.
            from baibai_engine.screening.calibration import store as calibration_store

            patcher = patch.dict(
                calibration_store.CACHE_SCHEMA_VERSIONS,
                {CALIBRATION_PANEL.name: "0" * 16},
            )
            patcher.start()
            self.addCleanup(patcher.stop)

            with self.assertRaisesRegex(CalibrationCacheError, "different transform"):
                read_panel(store_dir, ASOF)

    def test_a_forward_contract_change_leaves_the_panel_readable(self) -> None:
        # The reason the contract version is derived per dataset. A column added to the
        # forward rows says nothing about whether a stored panel is still the contract,
        # and a store-wide compatibility gate would answer that it is not — turning a
        # change to one dataset into a rebuild of all three.
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            result = build_panel(ASOF, sqlite_path=sqlite_path, rules=load_screening_rules())
            store_dir = Path(tmp) / "calibration"
            write_panel(store_dir, ASOF, result.rows, result.diagnostics)
            write_forward(store_dir, ASOF, [])
            from baibai_engine.screening.calibration import store as calibration_store

            patcher = patch.dict(
                calibration_store.CACHE_SCHEMA_VERSIONS,
                {CALIBRATION_FORWARD.name: "0" * 16},
            )
            patcher.start()
            self.addCleanup(patcher.stop)

            self.assertEqual(
                {row.ticker for row in read_panel(store_dir, ASOF)},
                {row.ticker for row in result.rows},
            )
            self.assertIsInstance(read_panel_meta(store_dir, ASOF), dict)
            with self.assertRaisesRegex(CalibrationCacheError, "different transform"):
                read_forward(store_dir, ASOF)

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

    def test_baseline_forward_rules_cannot_use_the_production_store(self) -> None:
        """The comparison baseline observes exits differently, so it gets its own store.

        Letting it write here would replace the requested range under other observation
        rules and leave the rest of the store measured under the default ones.
        """

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
                    use_control_event_exits=False,
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

    def test_failed_force_build_removes_its_unique_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            store_dir = Path(tmp) / "calibration"

            def fail_after_staging(root: Path, *_args: object, **_kwargs: object) -> None:
                root.mkdir(parents=True, exist_ok=True)
                (root / "partial").write_bytes(b"partial")
                raise CalibrationCacheError("injected write failure")

            with (
                patch(
                    "baibai_engine.screening.calibration.cli.month_end_asof_grid",
                    return_value=[ASOF],
                ),
                patch(
                    "baibai_engine.screening.calibration.cli.write_panel",
                    side_effect=fail_after_staging,
                ),
            ):
                code = calibration_build_command(
                    sqlite_path=sqlite_path,
                    calibration_dir=store_dir,
                    rules=load_screening_rules(),
                    start=ASOF,
                    end=ASOF,
                    force=True,
                )

            self.assertEqual(code, 1)
            self.assertEqual(list(Path(tmp).glob(".calibration.generation.*")), [])

    def test_failed_incremental_build_never_exposes_partial_cohorts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            store_dir = root / "calibration"
            with patch(
                "baibai_engine.screening.calibration.cli.month_end_asof_grid",
                return_value=[ASOF],
            ):
                self.assertEqual(
                    calibration_build_command(
                        sqlite_path=sqlite_path,
                        calibration_dir=store_dir,
                        rules=load_screening_rules(),
                        start=ASOF,
                        end=ASOF,
                    ),
                    0,
                )
            before = resolve_calibration_bundle(store_dir).ref

            with (
                patch(
                    "baibai_engine.screening.calibration.cli.month_end_asof_grid",
                    return_value=[ASOF, date(2026, 7, 31)],
                ),
                patch(
                    "baibai_engine.screening.calibration.cli.write_forward",
                    side_effect=CalibrationCacheError("injected late failure"),
                ),
            ):
                code = calibration_build_command(
                    sqlite_path=sqlite_path,
                    calibration_dir=store_dir,
                    rules=load_screening_rules(),
                    start=ASOF,
                    end=date(2026, 7, 31),
                )

            self.assertEqual(code, 1)
            self.assertEqual(resolve_calibration_bundle(store_dir).ref, before)
            self.assertEqual(list(root.glob(".calibration.generation.*")), [])

    def test_repeated_builds_do_not_accumulate_sealed_stores(self) -> None:
        """Storage must follow what the store publishes, not how many times it was built.

        Each build seals the whole legacy store to read it consistently. Keeping one per
        build would put roughly 2 GB of production data into the store every month while
        the cohorts themselves are a few hundred megabytes in total — the lake would
        cross its entire capacity objective in a handful of generations, and reachability
        would protect every copy, so collection could reclaim none of it.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            store_dir = root / "calibration"
            identities: set[str] = set()

            for run in range(3):
                with sqlite3.connect(sqlite_path) as connection:
                    connection.execute(
                        "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?,?,?)",
                        (f"90{run}0", "2026-06-29", 100.0 + run),
                    )
                with patch(
                    "baibai_engine.screening.calibration.cli.month_end_asof_grid",
                    return_value=[ASOF],
                ):
                    code = calibration_build_command(
                        sqlite_path=sqlite_path,
                        calibration_dir=store_dir,
                        rules=load_screening_rules(),
                        start=ASOF,
                        end=ASOF,
                        force=True,
                        stdout=io.StringIO(),
                    )
                self.assertEqual(code, 0)
                bundle = resolve_calibration_bundle(store_dir)
                identities.update(
                    source.source_id
                    for entry in bundle.cohorts.values()
                    for source in entry.panel.sources
                )

            # Three distinct generations were read and each was named in a manifest,
            # and none of them left bytes behind: the store stays smaller than one copy
            # of the legacy database rather than growing by one per build.
            self.assertEqual(len(identities), 3)
            self.assertEqual(list(store_dir.rglob("*.sqlite")), [])
            self.assertEqual(list(root.glob(".generation.*")), [])
            stored = sum(path.stat().st_size for path in store_dir.rglob("*") if path.is_file())
            self.assertLess(stored, sqlite_path.stat().st_size)

    def test_a_forced_build_refuses_to_drop_cohorts_outside_its_range(self) -> None:
        """A build states the window it recomputes, not the history it means to keep.

        Without ``--force`` the store is hard-linked into the work generation, so
        everything outside the window is carried. With it the generation starts empty,
        so a run meant to correct one month would publish a current bundle holding only
        that month — and the years it dropped would leave the served inventory with
        nothing saying so.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            store_dir = root / "calibration"
            earlier = "2026-05-29"
            publish_panel(store_dir, earlier, [{"ticker": "7203"}])
            publish_panel(store_dir, ASOF.isoformat(), [{"ticker": "7203"}])
            before = resolve_calibration_bundle(store_dir).ref.bundle_id

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
                    force=True,
                    stdout=io.StringIO(),
                )

            self.assertEqual(code, 1)
            self.assertIn(earlier, errors.getvalue())
            # Refused before adoption, so the served generation is untouched.
            self.assertEqual(resolve_calibration_bundle(store_dir).ref.bundle_id, before)
            self.assertEqual(published_cohorts(store_dir), [date.fromisoformat(earlier), ASOF])

    def test_a_forced_build_covering_the_whole_store_is_allowed(self) -> None:
        # The check is on what is about to become current, not on the flag: a forced
        # rebuild whose window covers the served inventory drops nothing and proceeds.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            store_dir = root / "calibration"
            publish_panel(store_dir, ASOF.isoformat(), [{"ticker": "7203"}])
            before = resolve_calibration_bundle(store_dir).ref.bundle_id

            with patch(
                "baibai_engine.screening.calibration.cli.month_end_asof_grid",
                return_value=[ASOF],
            ):
                code = calibration_build_command(
                    sqlite_path=sqlite_path,
                    calibration_dir=store_dir,
                    rules=load_screening_rules(),
                    start=ASOF,
                    end=ASOF,
                    force=True,
                    stdout=io.StringIO(),
                )

            self.assertEqual(code, 0)
            self.assertNotEqual(resolve_calibration_bundle(store_dir).ref.bundle_id, before)
            self.assertEqual(published_cohorts(store_dir), [ASOF])

    def test_an_unreadable_current_stops_every_build_into_that_store(self) -> None:
        """A store that cannot say what it serves is not a store to write into.

        Repairing it in place would mean rebuilding over live data from an inventory
        nothing can state. Every attempt refuses identically, including the one that
        follows a refusal, so the state a maintainer has to reason about is the one the
        store is actually in rather than one an earlier repair left behind.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            store_dir = root / "calibration"
            earlier = "2026-05-29"
            publish_panel(store_dir, earlier, [{"ticker": "7203"}])
            publish_panel(store_dir, ASOF.isoformat(), [{"ticker": "7203"}])
            pointer = store_dir / current_calibration_bundle_pointer_key()
            broken = b"{ not json"
            pointer.write_bytes(broken)

            for force in (False, True):
                errors = io.StringIO()
                with (
                    patch(
                        "baibai_engine.screening.calibration.cli.month_end_asof_grid",
                        return_value=[ASOF],
                    ),
                    contextlib.redirect_stderr(errors),
                ):
                    code = calibration_build_command(
                        sqlite_path=sqlite_path,
                        calibration_dir=store_dir,
                        rules=load_screening_rules(),
                        start=ASOF,
                        end=ASOF,
                        force=force,
                        stdout=io.StringIO(),
                    )
                self.assertEqual(code, 1)
                self.assertIn("separate --calibration-dir", errors.getvalue())

            # Nothing was moved, quarantined or written: the broken store is exactly as
            # the operator left it, which is what makes the directory swap reversible.
            self.assertEqual(pointer.read_bytes(), broken)

            # The way forward is a store of its own, which reads before it replaces
            # anything.
            replacement = root / "calibration-rebuild"
            with patch(
                "baibai_engine.screening.calibration.cli.month_end_asof_grid",
                return_value=[ASOF],
            ):
                rebuilt = calibration_build_command(
                    sqlite_path=sqlite_path,
                    calibration_dir=replacement,
                    rules=load_screening_rules(),
                    start=ASOF,
                    end=ASOF,
                    stdout=io.StringIO(),
                )

            self.assertEqual(rebuilt, 0)
            self.assertEqual(published_cohorts(replacement), [ASOF])

    def test_a_generation_a_killed_build_left_behind_is_discarded_and_reported(self) -> None:
        """A work generation is a sibling of the store, so nothing else would find it.

        It is built by hard-linking the store into it, which puts it outside every
        prefix the lake inventory and the collector walk. A build killed mid-run leaves
        one behind, and only the next build — which holds the writer lock, so no live
        generation can exist — is in a position to reclaim it.
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            store_dir = root / "calibration"
            abandoned = root / f".generation.{store_dir.name}.deadbeef"
            (abandoned / "lake").mkdir(parents=True)
            (abandoned / "lake" / "leftover.parquet").write_bytes(b"abandoned generation")
            output = io.StringIO()

            with patch(
                "baibai_engine.screening.calibration.cli.month_end_asof_grid",
                return_value=[ASOF],
            ):
                code = calibration_build_command(
                    sqlite_path=sqlite_path,
                    calibration_dir=store_dir,
                    rules=load_screening_rules(),
                    start=ASOF,
                    end=ASOF,
                    stdout=output,
                )

            self.assertEqual(code, 0)
            self.assertFalse(abandoned.exists())
            self.assertIn("discarded abandoned generation", output.getvalue())
            self.assertIn("20 linked bytes", output.getvalue())

    def test_build_closes_every_dataset_over_one_sqlite_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "market.sqlite"
            _build_fixture_sqlite(sqlite_path)
            store_dir = root / "calibration"
            with patch(
                "baibai_engine.screening.calibration.cli.month_end_asof_grid",
                return_value=[ASOF],
            ):
                code = calibration_build_command(
                    sqlite_path=sqlite_path,
                    calibration_dir=store_dir,
                    rules=load_screening_rules(),
                    start=ASOF,
                    end=ASOF,
                )

            self.assertEqual(code, 0)
            fixed = resolve_calibration_bundle(store_dir)
            manifests = list(fixed.datasets.values())
            source_sets = {
                cohort.sources
                for manifest in manifests
                for cohort in manifest.cohort_inventory.values()
            }
            self.assertEqual(len(source_sets), 1)
            sources = next(iter(source_sets))
            self.assertEqual(len(sources), 1)
            snapshot = sources[0]
            self.assertEqual(snapshot.kind, "sqlite_snapshot")
            # The seal is the whole legacy store and belongs to the operation that took
            # it, so what survives is the identity: the store the cohorts were built
            # from can be recognised, and no copy of it is kept per generation.
            self.assertEqual(
                snapshot.source_id,
                f"market-v{snapshot.schema_version}-{snapshot.sha256[:24]}",
            )
            self.assertEqual(list(store_dir.rglob("*.sqlite")), [])
            self.assertEqual(
                {manifest.producer_git_commit for manifest in manifests},
                {"a" * 40},
            )

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


class DerivedCacheIdentityTests(unittest.TestCase):
    """互換性を決める入力が動けば版も動く。手で進める判断を残さないための検査。"""

    def test_a_new_panel_column_moves_the_identity(self) -> None:
        from baibai_engine.screening.calibration import store as calibration_store

        before = calibration_store._derive_cache_schema_version()
        with patch.object(
            calibration_store,
            "PANEL_FIELD_NAMES",
            (*calibration_store.PANEL_FIELD_NAMES, "new_axis"),
        ):
            after = calibration_store._derive_cache_schema_version()
        self.assertNotEqual(before, after)

    def test_measuring_one_more_threshold_moves_the_identity(self) -> None:
        """列の形を変えずに観測の範囲だけ広げた変更が、実際に進め忘れを起こした形。"""
        from baibai_engine.screening.calibration import store as calibration_store

        before = calibration_store._derive_cache_schema_version()
        widened = {
            name: {**fields, "newly_measured_threshold": None}
            for name, fields in calibration_store.RELAXED.items()
        }
        with patch.object(calibration_store, "RELAXED", widened):
            after = calibration_store._derive_cache_schema_version()
        self.assertNotEqual(before, after)

    def test_changing_a_relaxed_value_moves_the_identity(self) -> None:
        """同じ閾値を別の値で測った cohort は互換でない。名前だけ見ると気付けない。"""
        from baibai_engine.screening.calibration import store as calibration_store

        before = calibration_store._derive_cache_schema_version()
        name, fields = next(iter(calibration_store.RELAXED.items()))
        field = next(iter(fields))
        retuned = {
            **calibration_store.RELAXED,
            name: {**fields, field: "retuned-sentinel"},
        }
        with patch.object(calibration_store, "RELAXED", retuned):
            after = calibration_store._derive_cache_schema_version()
        self.assertNotEqual(before, after)

    def test_a_new_valuation_revision_moves_the_identity(self) -> None:
        """式の意味の変更は内容から導けないので人が宣言するが、宣言すれば版も動く。"""
        from baibai_engine.screening.calibration import store as calibration_store

        before = calibration_store._derive_cache_schema_version()
        with patch.object(calibration_store, "VALUATION_CALCULATION_REVISION", "next-revision"):
            after = calibration_store._derive_cache_schema_version()
        self.assertNotEqual(before, after)

    def test_a_new_gate_axis_leaves_the_identity_alone(self) -> None:
        """評価軸は既存の panel 列を指すだけで、cache の中身を 1 バイトも変えない。

        版へ入れると 81 cohort の再構築を互換性上は不要な変更のたびに要求する。
        """
        from baibai_engine.screening.calibration import evaluation
        from baibai_engine.screening.calibration import store as calibration_store

        before = calibration_store._derive_cache_schema_version()
        with patch.object(evaluation, "GATE_BASE_AXES", (*evaluation.GATE_BASE_AXES, "p_s")):
            after = calibration_store._derive_cache_schema_version()
        self.assertEqual(before, after)

    def test_the_identity_is_stable_for_the_same_inputs(self) -> None:
        from baibai_engine.screening.calibration import store as calibration_store

        self.assertEqual(
            calibration_store._derive_cache_schema_version(),
            calibration_store._derive_cache_schema_version(),
        )


class GridDropRefusalTest(unittest.TestCase):
    """A too-narrow `--force` window is refused from the grid, before anything is built."""

    def _store_with_cohorts(self, directory: Path, asofs: tuple[str, ...]) -> None:
        for asof in asofs:
            publish_panel(directory, asof, [{"ticker": "1301"}])

    def test_a_narrow_force_window_is_refused_from_the_grid_alone(self) -> None:
        from baibai_engine.screening.calibration.cli import _cohorts_a_grid_would_drop

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            self._store_with_cohorts(directory, ("2024-01-31", "2024-02-29", "2024-03-29"))

            dropped = _cohorts_a_grid_would_drop(directory, [date(2024, 3, 29)], force=True)

        self.assertEqual(dropped, [date(2024, 1, 31), date(2024, 2, 29)])

    def test_a_window_covering_every_stored_cohort_is_allowed(self) -> None:
        from baibai_engine.screening.calibration.cli import _cohorts_a_grid_would_drop

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            self._store_with_cohorts(directory, ("2024-01-31", "2024-02-29"))

            dropped = _cohorts_a_grid_would_drop(
                directory, [date(2024, 1, 31), date(2024, 2, 29)], force=True
            )

        self.assertEqual(dropped, [])

    def test_an_incremental_build_carries_history_so_nothing_is_dropped(self) -> None:
        """Without `--force` the store is hard-linked in, so a narrow window keeps history."""

        from baibai_engine.screening.calibration.cli import _cohorts_a_grid_would_drop

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            self._store_with_cohorts(directory, ("2024-01-31", "2024-02-29"))

            dropped = _cohorts_a_grid_would_drop(directory, [date(2024, 2, 29)], force=False)

        self.assertEqual(dropped, [])
