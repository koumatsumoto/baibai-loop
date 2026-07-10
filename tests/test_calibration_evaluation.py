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

from baibai_loop.screening.calibration.cli import calibration_evaluate_command
from baibai_loop.screening.calibration.evaluation import (
    _spearman,
    evaluate_cohorts,
)
from baibai_loop.screening.calibration.forward import ForwardReturnRow
from baibai_loop.screening.calibration.panel import PanelDiagnostics, PanelRow
from baibai_loop.screening.calibration.store import write_forward, write_panel


def _panel_row(
    ticker: str,
    *,
    per_trailing: float | None,
    rank: int | None = None,
    dividend_yield: float | None = None,
    sector_33: str = "サービス業",
    er_annual: float | None = None,
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
        er_reversion_annual=None,
        er_carry_annual=None,
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


def _panel_diagnostics() -> PanelDiagnostics:
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

    def test_dividend_accrual_shifts_total_return_excess(self) -> None:
        # 全銘柄 price return 0 の中で、配当利回り 4% の銘柄だけが 6m で
        # +2% (= 0.04 x 0.5) の total return を持つ。中央値 (=0 近傍) に対する
        # excess が accrual 分だけ正になることを確認する。
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
        self.assertEqual(cohorts[0]["dividend_yield_coverage"], 120)
        axes = cohorts[0]["axes"]
        assert isinstance(axes, dict)
        dy_axis = axes["dividend_yield"]
        assert isinstance(dy_axis, dict)
        deciles = dy_axis["deciles"]
        assert isinstance(deciles, list)
        best = deciles[-1]
        assert isinstance(best, dict)
        # best decile (配当あり銘柄を含む) の mean excess は accrual 分 > 0
        mean_excess = best["mean_excess"]
        assert isinstance(mean_excess, float)
        self.assertGreater(mean_excess, 0.0)

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

    def test_cohort_skipped_when_population_too_small(self) -> None:
        panel = [_panel_row("1000", per_trailing=10.0)]
        forwards = [_forward_row("1000", 0.1)]
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        horizon = result["6m"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        self.assertEqual(cohorts, [])

    def test_sector_subset_diagnostics_compare_best_decile_trap_to_all_population(
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
            sector_subset=["機械"],
        )

        horizon = result["6m"]
        assert isinstance(horizon, dict)
        diagnostics = horizon["sector_subset_diagnostics"]
        assert isinstance(diagnostics, dict)
        self.assertEqual(diagnostics["sectors"], ["機械"])
        cohorts = diagnostics["cohorts"]
        assert isinstance(cohorts, list)
        self.assertEqual(len(cohorts), 1)
        self.assertEqual(cohorts[0]["subset_population_resolved"], 120)
        axes = cohorts[0]["axes"]
        assert isinstance(axes, dict)
        per_axis = axes["per_trailing"]
        assert isinstance(per_axis, dict)
        self.assertEqual(per_axis["n"], 120)
        self.assertEqual(per_axis["rank_ic"], 1.0)
        self.assertEqual(per_axis["best_decile_trap_rate"], 0.0)
        self.assertEqual(per_axis["all_population_best_decile_trap_rate"], 1.0)
        self.assertEqual(per_axis["best_decile_trap_rate_delta_vs_all_population"], -1.0)

        aggregate = diagnostics["aggregate"]
        assert isinstance(aggregate, dict)
        aggregate_axes = aggregate["axes"]
        assert isinstance(aggregate_axes, dict)
        aggregate_per = aggregate_axes["per_trailing"]
        assert isinstance(aggregate_per, dict)
        self.assertEqual(aggregate_per["mean_rank_ic"], 1.0)
        self.assertEqual(aggregate_per["mean_best_decile_trap_rate"], 0.0)
        self.assertEqual(aggregate_per["mean_all_population_best_decile_trap_rate"], 1.0)
        self.assertEqual(aggregate_per["mean_best_decile_trap_rate_delta_vs_all_population"], -1.0)

    def test_sector_subset_diagnostics_are_absent_without_sector_subset(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(120):
            ticker = f"6{i:03d}"
            panel.append(_panel_row(ticker, per_trailing=10.0 + i))
            forwards.append(_forward_row(ticker, 0.001 * i))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])

        horizon = result["6m"]
        assert isinstance(horizon, dict)
        self.assertNotIn("sector_subset_diagnostics", horizon)

    def test_sector_subset_diagnostics_allow_subset_below_axis_sample_minimum(
        self,
    ) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(60):
            ticker = f"7{i:03d}"
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=5.0 + i * 0.05,
                    sector_33="銀行業",
                )
            )
            forwards.append(_forward_row(ticker, 0.25 - i * 0.001))
        for i in range(60):
            ticker = f"8{i:03d}"
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=20.0 + i * 0.05,
                    sector_33="機械",
                )
            )
            forwards.append(_forward_row(ticker, -0.25 - i * 0.001))

        result = evaluate_cohorts(
            {"2025-06-30": panel},
            {"2025-06-30": forwards},
            horizons=["6m"],
            sector_subset=["銀行業"],
            sector_subset_axes=["per_trailing"],
        )

        horizon = result["6m"]
        assert isinstance(horizon, dict)
        diagnostics = horizon["sector_subset_diagnostics"]
        assert isinstance(diagnostics, dict)
        self.assertEqual(diagnostics["axes"], ["per_trailing"])
        aggregate = diagnostics["aggregate"]
        assert isinstance(aggregate, dict)
        aggregate_axes = aggregate["axes"]
        assert isinstance(aggregate_axes, dict)
        per_axis = aggregate_axes["per_trailing"]
        assert isinstance(per_axis, dict)
        self.assertEqual(per_axis["cohorts"], 1)
        self.assertEqual(per_axis["mean_n"], 60.0)
        self.assertEqual(per_axis["mean_rank_ic"], 1.0)

    def test_calibration_evaluate_command_writes_sector_subset_yaml(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(60):
            ticker = f"7{i:03d}"
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=10.0 + i * 0.05,
                    sector_33="銀行業",
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
                sector_subset=["financial"],
                sector_subset_axes=["per_trailing"],
                output_path=output_path,
                stdout=StringIO(),
            )

            self.assertEqual(exit_code, 0)
            payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
            assert isinstance(payload, dict)
            self.assertEqual(
                payload["sector_subset"],
                {
                    "sectors": ["銀行業", "証券・商品先物取引業", "保険業", "その他金融業"],
                    "axes": ["per_trailing"],
                },
            )
            results = payload["results"]
            assert isinstance(results, dict)
            horizon = results["6m"]
            assert isinstance(horizon, dict)
            diagnostics = horizon["sector_subset_diagnostics"]
            assert isinstance(diagnostics, dict)
            self.assertEqual(
                diagnostics["sectors"],
                ["銀行業", "証券・商品先物取引業", "保険業", "その他金融業"],
            )
            self.assertEqual(diagnostics["axes"], ["per_trailing"])
            aggregate = diagnostics["aggregate"]
            assert isinstance(aggregate, dict)
            self.assertEqual(aggregate["cohort_count"], 1)


if __name__ == "__main__":
    unittest.main()
