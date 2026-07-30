from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.calibration.cli import calibration_evaluate_command
from baibai_engine.screening.calibration.evaluation import (
    _reversion_plus_capped_carry,
    _spearman,
    evaluate_cohorts,
)
from baibai_engine.screening.calibration.forward import ForwardReturnRow
from baibai_engine.screening.calibration.panel import PanelDiagnostics, PanelRow
from baibai_engine.screening.calibration.store import write_forward, write_panel


def _panel_row(
    ticker: str,
    *,
    per_trailing: float | None,
    rank: int | None = None,
    dividend_yield: float | None = None,
    sector_33: str = "サービス業",
    er_annual: float | None = None,
    er_reversion_annual: float | None = None,
    er_carry_annual: float | None = None,
) -> PanelRow:
    return PanelRow(
        asof="2025-06-30",
        ticker=ticker,
        sector_33=sector_33,
        in_population=True,
        market_cap_oku=500.0,
        avg_turnover_oku=5.0,
        listing_span_days=1200,
        close=1000.0,
        per_forward=None,
        per_trailing=per_trailing,
        pbr=None,
        ev_ebitda=None,
        p_s=None,
        pcfr=None,
        ocf_yield=None,
        fcf_yield=None,
        net_cash_to_market_cap=None,
        cash_to_market_cap=None,
        equity_ratio=None,
        price_to_equity=None,
        dividend_yield=dividend_yield,
        eps_yoy=None,
        sales_yoy=None,
        operating_profit_yoy=None,
        cfo_yoy=None,
        accruals_to_assets=None,
        net_share_change_yoy=None,
        ttm_quality_per_trailing="exact",
        ttm_quality_ocf_yield="exact",
        price_change_60d=None,
        gap_from_52w_low=None,
        price_history_coverage_750d=1.0,
        smg_per_forward=None,
        smg_per_trailing=None,
        smg_pbr=None,
        smg_ev_ebitda=None,
        smg_p_s=None,
        srp_per_forward=None,
        srp_per_trailing=None,
        srp_pbr=None,
        srp_ev_ebitda=None,
        srp_p_s=None,
        er_annual=er_annual,
        er_reversion_annual=er_reversion_annual,
        er_carry_annual=er_carry_annual,
        er_upside_capped=None,
        pass_screen=rank is not None,
        evidence_playbooks="",
        selection_rank=rank,
        recommended_rank=rank,
    )


def _forward_row(ticker: str, price_return: float) -> ForwardReturnRow:
    return ForwardReturnRow(
        asof="2025-06-30",
        ticker=ticker,
        horizon="6m",
        target_date="2025-12-29",
        resolved=True,
        price_return=price_return,
        stale_price=False,
        entry_date="2025-06-30",
        exit_date="2025-12-29",
    )


def _panel_diagnostics(
    *,
    master_snapshot_status: str = "unavailable",
    survivorship_coverage_status: str = "unavailable",
) -> PanelDiagnostics:
    return PanelDiagnostics(
        asof="2025-06-30",
        rules_hash="rules-hash",
        universe_size=240,
        population_size=240,
        candidates=0,
        evidence_candidates=0,
        bars_tickers_not_in_master=0,
        effective_bars_start="2024-01-01",
        effective_fin_start="2024-01-01",
        bars_window_clamped=False,
        fin_window_clamped=False,
        population_per_trailing_nonnull=240,
        population_pbr_nonnull=0,
        population_ocf_yield_nonnull=0,
        population_per_trailing_exact=240,
        master_snapshot_status=master_snapshot_status,
        survivorship_coverage_status=survivorship_coverage_status,
    )


class SpearmanTest(unittest.TestCase):
    def test_spearman_perfect_monotone_is_one(self) -> None:
        pairs = [(float(i), float(i * 2)) for i in range(40)]
        ic = _spearman(pairs)
        assert ic is not None
        self.assertAlmostEqual(ic, 1.0)

    def test_spearman_perfect_inverse_is_minus_one(self) -> None:
        pairs = [(float(i), float(-i)) for i in range(40)]
        ic = _spearman(pairs)
        assert ic is not None
        self.assertAlmostEqual(ic, -1.0)

    def test_spearman_below_min_sample_is_none(self) -> None:
        self.assertIsNone(_spearman([(1.0, 1.0)] * 10))


class EvaluateCohortsTest(unittest.TestCase):
    def test_cheap_per_trailing_outperformance_yields_positive_ic(self) -> None:
        # 150 銘柄: PER が低いほど forward return が高い設計 (direction=-1 で
        # 正の IC・best decile 正の超過になるべき) 。上位 10 銘柄を select 順に見立てる。
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        n = 150
        for i in range(n):
            ticker = f"{1000 + i}"
            per = 5.0 + i * 0.5  # 銘柄 i の PER: 安い順
            ret = 0.30 - 0.004 * i  # 安いほど高リターン
            rank = i + 1 if i < 10 else None
            panel.append(_panel_row(ticker, per_trailing=per, rank=rank))
            forwards.append(_forward_row(ticker, ret))
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        horizon = result["6m"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        self.assertEqual(len(cohorts), 1)
        axes = cohorts[0]["axes"]
        assert isinstance(axes, dict)
        per_axis = axes["per_trailing"]
        assert isinstance(per_axis, dict)
        rank_ic = per_axis["rank_ic"]
        assert isinstance(rank_ic, float)
        self.assertAlmostEqual(rank_ic, 1.0)
        best_median = per_axis["best_decile_median_excess"]
        assert isinstance(best_median, float)
        self.assertGreater(best_median, 0.0)
        spread = per_axis["decile_spread_median"]
        assert isinstance(spread, float)
        self.assertGreater(spread, 0.0)
        # selection replay: 上位 10 銘柄は安い側なので正の超過。
        selection = cohorts[0]["selection"]
        assert isinstance(selection, dict)
        top10 = selection["recommended_rank_top10"]
        assert isinstance(top10, dict)
        self.assertEqual(top10["n"], 10)
        median_excess = top10["median_excess"]
        assert isinstance(median_excess, float)
        self.assertGreater(median_excess, 0.0)
        # aggregate 構造: 1 cohort・IC 勝率 1.0。
        aggregate = horizon["aggregate"]
        assert isinstance(aggregate, dict)
        self.assertEqual(aggregate["cohort_count"], 1)

    def test_price_return_only_does_not_accrue_dividend_yield(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(120):
            ticker = f"{2000 + i}"
            dy = 0.04 if i == 0 else 0.0
            panel.append(_panel_row(ticker, per_trailing=10.0, dividend_yield=dy))
            forwards.append(_forward_row(ticker, 0.0))
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        horizon = result["6m"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        self.assertEqual(len(cohorts), 1)
        self.assertEqual(cohorts[0]["metric_basis"], "price_return_only")
        axes = cohorts[0]["axes"]
        assert isinstance(axes, dict)
        dy_axis = axes["dividend_yield"]
        assert isinstance(dy_axis, dict)
        deciles = dy_axis["deciles"]
        assert isinstance(deciles, list)
        best = deciles[-1]
        assert isinstance(best, dict)
        # 配当利回りだけでは price return diagnostic の excess を作らない。
        mean_excess = best["mean_excess"]
        assert isinstance(mean_excess, float)
        self.assertEqual(mean_excess, 0.0)

    def test_er_ranked_virtual_replay_orders_by_er_within_screen_passers(self) -> None:
        # screen 通過 20 銘柄に er_annual を 0.01..0.20 で与え、er と forward return を
        # 逆相関にする → er_ranked_top5 は er 上位 = 低リターン側を選ぶので
        # er_population_top5 と一致し、excess は選抜どおりの値になる。
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        n = 120
        for i in range(n):
            ticker = f"{3000 + i}"
            row = _panel_row(ticker, per_trailing=10.0, rank=(i + 1 if i < 20 else None))
            # PanelRow は frozen なので必要 field を差し替えた新 instance を作る。
            row = replace(row, er_annual=0.20 - 0.001 * i, pass_screen=i < 20)
            panel.append(row)
            forwards.append(_forward_row(ticker, 0.001 * i))
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        horizon = result["6m"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        selection = cohorts[0]["selection"]
        assert isinstance(selection, dict)
        er_top5 = selection["er_ranked_top5"]
        assert isinstance(er_top5, dict)
        # er 最上位 5 銘柄 = i=0..4 = return 最低群 → 負の excess
        self.assertEqual(er_top5["n"], 5)
        median_excess = er_top5["median_excess"]
        assert isinstance(median_excess, float)
        self.assertLess(median_excess, 0.0)
        # er_population は pass_screen に依らないが、er 順は同じ i=0..4。
        pop_top5 = selection["er_population_top5"]
        assert isinstance(pop_top5, dict)
        self.assertEqual(pop_top5["median_excess"], er_top5["median_excess"])

    def test_reversion_ranked_virtual_replay_orders_by_reversion_not_er(self) -> None:
        # screen 通過 20 銘柄で er_annual と er_reversion_annual の順位を逆にし、
        # forward return を reversion 順に揃える → reversion_ranked_top5 は
        # er_ranked_top5 (carry が押し上げた高 er 群 = 低リターン側) を上回る。
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        n = 120
        for i in range(n):
            ticker = f"{3000 + i}"
            row = _panel_row(ticker, per_trailing=10.0, rank=(i + 1 if i < 20 else None))
            if i < 20:
                row = replace(
                    row,
                    er_annual=0.02 + 0.004 * i,
                    er_reversion_annual=0.02 - 0.001 * i,
                    er_carry_annual=0.005 * i,
                    pass_screen=True,
                )
            panel.append(row)
            forwards.append(_forward_row(ticker, -0.001 * i))
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        horizon = result["6m"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        selection = cohorts[0]["selection"]
        assert isinstance(selection, dict)
        reversion_top5 = selection["reversion_ranked_top5"]
        er_top5 = selection["er_ranked_top5"]
        assert isinstance(reversion_top5, dict)
        assert isinstance(er_top5, dict)
        self.assertEqual(reversion_top5["n"], 5)
        reversion_median = reversion_top5["median_excess"]
        er_median = er_top5["median_excess"]
        assert isinstance(reversion_median, float)
        assert isinstance(er_median, float)
        # reversion top-5 は i=0..4 (return 最高群)、er top-5 は i=19..15 (最低群)。
        self.assertGreater(reversion_median, er_median)
        view_top5 = selection["reversion_carry_ranked_top5"]
        assert isinstance(view_top5, dict)
        self.assertEqual(view_top5["n"], 5)

    def test_reversion_carry_key_caps_carry_contribution(self) -> None:
        base = _panel_row("9999", per_trailing=10.0, rank=1)
        capped = replace(base, er_reversion_annual=0.01, er_carry_annual=0.94)
        uncapped = replace(base, er_reversion_annual=0.01, er_carry_annual=0.10)
        missing_carry = replace(base, er_reversion_annual=0.01, er_carry_annual=None)
        self.assertAlmostEqual(_reversion_plus_capped_carry(capped), 0.01 + 0.5 * 0.15)
        self.assertAlmostEqual(_reversion_plus_capped_carry(uncapped), 0.01 + 0.5 * 0.10)
        self.assertAlmostEqual(_reversion_plus_capped_carry(missing_carry), 0.01)

    def test_er_calibration_compares_centered_reversion_with_centered_price_return(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(100):
            ticker = f"35{i:02d}"
            reversion = i / 1000
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=10.0,
                    er_annual=reversion + 0.03,
                    er_reversion_annual=reversion,
                    er_carry_annual=0.03,
                )
            )
            forwards.append(_forward_row(ticker, i / 1000))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])

        calibration = result["6m"]["cohorts"][0]["er_calibration"]
        assert isinstance(calibration, dict)
        self.assertEqual(
            calibration["prediction_basis"],
            "er_reversion_annual_relative_to_population_median",
        )
        self.assertEqual(
            calibration["realized_basis"], "price_return_relative_to_population_median"
        )
        self.assertEqual(calibration["calibration_error_basis"], "realized_minus_predicted")
        self.assertEqual(calibration["population_median_reversion_annual"], 0.0495)
        quintiles = calibration["er_quintiles"]
        assert isinstance(quintiles, list)
        first = quintiles[0]
        assert isinstance(first, dict)
        self.assertEqual(first["median_predicted_reversion_excess"], -0.02)
        self.assertEqual(first["median_realized_price_excess"], -0.04)
        self.assertEqual(first["calibration_error"], -0.02)

    def test_er_calibration_is_unchanged_when_only_carry_changes(self) -> None:
        forwards = [_forward_row(f"36{i:02d}", i / 1000) for i in range(100)]

        def panel_with_carry(carry: float) -> list[PanelRow]:
            return [
                _panel_row(
                    f"36{i:02d}",
                    per_trailing=10.0,
                    er_annual=i / 1000 + carry,
                    er_reversion_annual=i / 1000,
                    er_carry_annual=carry,
                )
                for i in range(100)
            ]

        low_carry = evaluate_cohorts(
            {"2025-06-30": panel_with_carry(0.01)},
            {"2025-06-30": forwards},
            horizons=["6m"],
        )
        high_carry = evaluate_cohorts(
            {"2025-06-30": panel_with_carry(0.20)},
            {"2025-06-30": forwards},
            horizons=["6m"],
        )

        self.assertEqual(
            low_carry["6m"]["cohorts"][0]["er_calibration"],
            high_carry["6m"]["cohorts"][0]["er_calibration"],
        )

    def test_cohort_with_insufficient_sample_remains_explicitly_unresolved(self) -> None:
        panel = [_panel_row("1000", per_trailing=10.0)]
        forwards = [_forward_row("1000", 0.1)]
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        horizon = result["6m"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        self.assertEqual(len(cohorts), 1)
        self.assertEqual(cohorts[0]["metric_calculation_status"], "unresolved")

    def test_candidate_partition_requires_exact_ticker_membership(self) -> None:
        panel = [_panel_row("1000", per_trailing=10.0), _panel_row("1001", per_trailing=10.0)]
        forwards = [_forward_row("9998", 0.1), _forward_row("9999", 0.1)]
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]
        assert isinstance(cohort, dict)
        coverage = cohort["coverage"]
        assert isinstance(coverage, dict)
        self.assertFalse(coverage["candidate_partition_complete"])
        self.assertEqual(coverage["candidate_forward_missing_count"], 2)
        self.assertEqual(coverage["candidate_forward_extra_count"], 2)

    def test_removed_per_sector_diagnostics_are_not_emitted(
        self,
    ) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(120):
            ticker = f"4{i:03d}"
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=10.0 + i * 0.05,
                    sector_33="機械",
                )
            )
            forwards.append(_forward_row(ticker, 0.30 - i * 0.001))
        for i in range(120):
            ticker = f"5{i:03d}"
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=4.0 + i * 0.05,
                    sector_33="サービス業",
                )
            )
            forwards.append(_forward_row(ticker, -0.30 - i * 0.001))

        result = evaluate_cohorts(
            {"2025-06-30": panel},
            {"2025-06-30": forwards},
            horizons=["6m"],
        )

        horizon = result["6m"]
        assert isinstance(horizon, dict)
        self.assertEqual(set(horizon), {"authority", "cohorts", "aggregate"})

    def test_horizon_output_has_only_current_contract_sections(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(120):
            ticker = f"6{i:03d}"
            panel.append(_panel_row(ticker, per_trailing=10.0 + i))
            forwards.append(_forward_row(ticker, 0.001 * i))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])

        horizon = result["6m"]
        assert isinstance(horizon, dict)
        self.assertEqual(set(horizon), {"authority", "cohorts", "aggregate"})

    def test_calibration_evaluate_command_marks_short_horizon_as_diagnostic(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(60):
            ticker = f"7{i:03d}"
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=10.0 + i * 0.05,
                    sector_33="サービス業",
                    er_annual=i / 1000 + 0.03,
                    er_reversion_annual=i / 1000,
                    er_carry_annual=0.03,
                )
            )
            forwards.append(_forward_row(ticker, 0.30 - i * 0.001))
        for i in range(60):
            ticker = f"8{i:03d}"
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=4.0 + i * 0.05,
                    sector_33="サービス業",
                    er_annual=(60 + i) / 1000 + 0.03,
                    er_reversion_annual=(60 + i) / 1000,
                    er_carry_annual=0.03,
                )
            )
            forwards.append(_forward_row(ticker, -0.30 - i * 0.001))

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_panel(root, date(2025, 6, 30), tuple(panel), _panel_diagnostics())
            write_forward(root, date(2025, 6, 30), forwards)
            output_path = root / "evaluation.yaml"

            exit_code = calibration_evaluate_command(
                calibration_dir=root,
                horizons=["6m"],
                output_path=output_path,
                stdout=StringIO(),
            )

            self.assertEqual(exit_code, 0)
            payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
            assert isinstance(payload, dict)
            self.assertEqual(payload["metric_basis"], "price_return_only")
            decision = payload["production_decision"]
            assert isinstance(decision, dict)
            self.assertFalse(decision["production_change_allowed"])
            results = payload["results"]
            assert isinstance(results, dict)
            horizon = results["6m"]
            assert isinstance(horizon, dict)
            aggregate = horizon["aggregate"]
            assert isinstance(aggregate, dict)
            self.assertEqual(aggregate["cohort_count"], 1)
            cohorts = horizon["cohorts"]
            assert isinstance(cohorts, list)
            calibration = cohorts[0]["er_calibration"]
            assert isinstance(calibration, dict)
            self.assertEqual(
                calibration["prediction_basis"],
                "er_reversion_annual_relative_to_population_median",
            )
            self.assertEqual(
                calibration["realized_basis"],
                "price_return_relative_to_population_median",
            )
            self.assertEqual(calibration["calibration_error_basis"], "realized_minus_predicted")

    def _long_horizon_cohort(
        self, extra: list[ForwardReturnRow]
    ) -> tuple[list[PanelRow], list[ForwardReturnRow]]:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(120):
            ticker = f"7{i:03d}"
            panel.append(_panel_row(ticker, per_trailing=10.0 + i * 0.05))
            forwards.append(
                replace(
                    _forward_row(ticker, 0.10 - i * 0.001),
                    horizon="3y",
                    target_date="2028-06-30",
                    exit_date="2028-06-30",
                    adjustment_factor_coverage="complete",
                )
            )
        for row in extra:
            panel.append(_panel_row(row.ticker, per_trailing=None))
            forwards.append(row)
        return panel, forwards

    def _reason_counts(
        self, panel: list[PanelRow], forwards: list[ForwardReturnRow]
    ) -> tuple[dict[str, int], dict[str, object]]:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_panel(
                root,
                date(2025, 6, 30),
                tuple(panel),
                _panel_diagnostics(
                    master_snapshot_status="exact_date",
                    survivorship_coverage_status="complete",
                ),
            )
            write_forward(root, date(2025, 6, 30), forwards)
            output_path = root / "evaluation.yaml"
            exit_code = calibration_evaluate_command(
                calibration_dir=root,
                horizons=["3y"],
                output_path=output_path,
                stdout=StringIO(),
            )
            self.assertEqual(exit_code, 0)
            payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        authority = payload["authority_coverage"]
        assert isinstance(authority, dict)
        counts = authority["reason_counts"]
        assert isinstance(counts, dict)
        cohorts = payload["results"]["3y"]["cohorts"]
        assert isinstance(cohorts, list)
        coverage = cohorts[0]["coverage"]
        assert isinstance(coverage, dict)
        return counts, coverage

    def test_not_listed_entry_is_disclosed_without_blocking_the_cohort(self) -> None:
        # asof に未上場だった銘柄の除外は正しいので、blocker にはならず件数だけ残る。
        not_listed = ForwardReturnRow(
            asof="2025-06-30",
            ticker="9100",
            horizon="3y",
            target_date="2028-06-30",
            resolved=False,
            price_return=None,
            stale_price=False,
            entry_date=None,
            exit_date=None,
            status="unresolved_missing_entry",
        )
        counts, coverage = self._reason_counts(*self._long_horizon_cohort([not_listed]))

        self.assertEqual(coverage["entry_not_listed_count"], 1)
        self.assertEqual(coverage["entry_price_gap_count"], 0)
        self.assertEqual(coverage["unpriced_exit_count"], 0)
        self.assertEqual([key for key in counts if key.startswith("entry_price_gap")], [])
        self.assertEqual([key for key in counts if key.startswith("unpriced_exit")], [])

    def test_price_gap_and_unpriced_exit_are_named_as_separate_blockers(self) -> None:
        # 取引可能名の取りこぼしと廃止 exit value の欠落は、別々の理由として名指しする。
        price_gap = ForwardReturnRow(
            asof="2025-06-30",
            ticker="9200",
            horizon="3y",
            target_date="2028-06-30",
            resolved=False,
            price_return=None,
            stale_price=False,
            entry_date="2024-01-31",
            exit_date=None,
            status="unresolved_missing_entry",
        )
        unpriced_exit = ForwardReturnRow(
            asof="2025-06-30",
            ticker="9300",
            horizon="3y",
            target_date="2028-06-30",
            resolved=False,
            price_return=None,
            stale_price=True,
            entry_date="2025-06-30",
            exit_date="2026-02-27",
            status="unresolved_stale_exit",
        )
        counts, coverage = self._reason_counts(
            *self._long_horizon_cohort([price_gap, unpriced_exit])
        )

        self.assertEqual(coverage["entry_price_gap_count"], 1)
        self.assertEqual(coverage["unpriced_exit_count"], 1)
        self.assertEqual(counts.get("entry_price_gap:1"), 1)
        self.assertEqual(counts.get("unpriced_exit:1"), 1)
        self.assertEqual(coverage["delisting_coverage_status"], "unpriced_exit")

    def test_production_decision_requires_explicit_core_scope(self) -> None:
        with TemporaryDirectory() as temp_dir:
            exit_code = calibration_evaluate_command(
                calibration_dir=Path(temp_dir),
                run_purpose="production_decision",
                stdout=StringIO(),
            )
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
