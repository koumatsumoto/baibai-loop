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

from baibai_engine.screening.calibration.cli import (
    _required_metric_statuses,
    calibration_evaluate_command,
)
from baibai_engine.screening.calibration.evaluation import (
    DECILES,
    MIN_AXIS_SAMPLE,
    _margin_deadline_gate_adoption_sign,
    _metric_direction_stability,
    _reversion_plus_capped_carry,
    _spearman,
    evaluate_cohorts,
)
from baibai_engine.screening.calibration.forward import ForwardReturnRow
from baibai_engine.screening.calibration.panel import (
    PanelDiagnostics,
    PanelRow,
    _with_mcap_quintile_percentiles,
)
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
    close: float | None = 1000.0,
    market_cap_oku: float | None = 500.0,
    avg_turnover_oku: float | None = 5.0,
    pbr: float | None = None,
    net_cash_to_market_cap: float | None = None,
    investment_securities: float | None = None,
    asset_backed_ratio: float | None = None,
    equity_ratio: float | None = None,
    shareholder_return_change: bool | None = None,
    price_change_60d: float | None = None,
    quality_signal_count: int | None = None,
    quality_cfo_positive: bool | None = None,
    quality_no_dilution: bool | None = None,
    realized_volatility_60d: float | None = None,
) -> PanelRow:
    return PanelRow(
        asof="2025-06-30",
        ticker=ticker,
        sector_33=sector_33,
        in_population=True,
        market_cap_oku=market_cap_oku,
        avg_turnover_oku=avg_turnover_oku,
        listing_span_days=1200,
        close=close,
        per_forward=None,
        per_trailing=per_trailing,
        pbr=pbr,
        ev_ebitda=None,
        p_s=None,
        pcfr=None,
        ocf_yield=None,
        fcf_yield=None,
        net_cash_to_market_cap=net_cash_to_market_cap,
        cash_to_market_cap=None,
        investment_securities=investment_securities,
        asset_backed_ratio=asset_backed_ratio,
        equity_ratio=equity_ratio,
        price_to_equity=None,
        dividend_yield=dividend_yield,
        eps_yoy=None,
        sales_yoy=None,
        operating_profit_yoy=None,
        cfo_yoy=None,
        accruals_to_assets=None,
        net_share_change_yoy=None,
        quality_roa_positive=None,
        quality_delta_roa_positive=None,
        quality_cfo_positive=quality_cfo_positive,
        quality_accrual_healthy=None,
        quality_delta_operating_margin_positive=None,
        quality_delta_equity_ratio_positive=None,
        quality_no_dilution=quality_no_dilution,
        quality_delta_asset_turnover_positive=None,
        quality_signal_available_count=8 if quality_signal_count is not None else 0,
        quality_signal_count=quality_signal_count,
        ttm_quality_per_trailing="exact",
        ttm_quality_ocf_yield="exact",
        price_change_60d=price_change_60d,
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
        margin_week_end=None,
        margin_long_to_adv=None,
        margin_long_share=None,
        margin_long_delta_26w=None,
        margin_std_long_share=None,
        pass_screen=rank is not None,
        evidence_playbooks="",
        selection_rank=rank,
        recommended_rank=rank,
        shareholder_return_change=shareholder_return_change,
        realized_volatility_60d=realized_volatility_60d,
    )


def _forward_row(
    ticker: str,
    price_return: float,
    *,
    total_return: float | None = None,
    horizon: str = "6m",
) -> ForwardReturnRow:
    return ForwardReturnRow(
        asof="2025-06-30",
        ticker=ticker,
        horizon=horizon,
        target_date="2025-12-29",
        resolved=True,
        price_return=price_return,
        stale_price=False,
        entry_date="2025-06-30",
        exit_date="2025-12-29",
        realized_dividend_sum=(10.0 if total_return is not None else None),
        realized_dividend_fy_count=(1 if total_return is not None else 0),
        total_return=total_return,
        total_return_status=(
            "resolved" if total_return is not None else "unresolved_missing_dividend"
        ),
    )


def _panel_diagnostics(
    *,
    master_snapshot_status: str = "unavailable",
    asof_population_mismatch_count: int = 0,
    priced_master_without_universe_count: int = 0,
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
        asof_population_mismatch_count=asof_population_mismatch_count,
        priced_master_without_universe_count=priced_master_without_universe_count,
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


class RequiredMetricStatusTest(unittest.TestCase):
    def test_missing_non_mapping_and_unknown_statuses_fail_closed(self) -> None:
        required = ("er_calibration", "er_level_calibration")
        self.assertEqual(
            _required_metric_statuses({"er_calibration": "eligible"}, required),
            {"er_calibration": "eligible", "er_level_calibration": "unresolved"},
        )
        self.assertEqual(
            _required_metric_statuses(None, required),
            {"er_calibration": "unresolved", "er_level_calibration": "unresolved"},
        )
        self.assertEqual(
            _required_metric_statuses(
                {"er_calibration": "eligible", "er_level_calibration": "claimed"}, required
            ),
            {"er_calibration": "eligible", "er_level_calibration": "unresolved"},
        )


class EvaluateCohortsTest(unittest.TestCase):
    def test_margin_deadline_gate_recompacts_the_rank_and_improves_traps(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for index in range(120):
            ticker = f"{3000 + index}"
            rank = index + 1 if index < 20 else None
            panel.append(
                replace(
                    _panel_row(ticker, per_trailing=10.0, rank=rank),
                    margin_std_long_share=0.9 if index < 2 else 0.5,
                )
            )
            realized = -0.5 if index < 2 else 0.2 if index in {10, 11} else 0.0
            forwards.append(_forward_row(ticker, realized))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]
        gate = cohort["margin_deadline_gate"]
        top10 = gate["top10"]

        self.assertTrue(top10["complete"])
        self.assertTrue(top10["changed"])
        self.assertEqual(gate["excluded_count"], 2)
        self.assertLess(top10["trap_rate_delta"], 0)
        self.assertEqual(cohort["metric_statuses"]["margin_deadline_gate_top10"], "eligible")

    def test_new_margin_axes_and_every_registered_control_are_reported(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for index in range(150):
            ticker = f"{4000 + index}"
            axis = float(index + 1)
            panel.append(
                replace(
                    _panel_row(
                        ticker,
                        per_trailing=5.0 + index / 10,
                        dividend_yield=index / 1000,
                        sector_33=f"sector-{index % 5}",
                        close=100.0 + index,
                        market_cap_oku=100.0 + index,
                        avg_turnover_oku=1.0 + index / 10,
                        price_change_60d=index / 1000,
                    ),
                    margin_short_to_adv=axis,
                    margin_long_to_adv_mcap_quintile_percentile=index / 149,
                    realized_volatility_60d=0.1 + index / 1000,
                )
            )
            forwards.append(_forward_row(ticker, -index / 100))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]
        axes = cohort["axes"]
        self.assertGreater(axes["margin_short_to_adv"]["decile_spread_median"], 0)
        hypotheses = cohort["margin_supply_demand_hypotheses"]
        short_controls = hypotheses["margin_short_to_adv"]["controls"]
        self.assertEqual(
            set(short_controls),
            {
                "market_cap_oku",
                "avg_turnover_oku",
                "per_trailing",
                "dividend_yield",
                "close",
                "price_change_60d",
                "realized_volatility_60d",
                "sector_33",
            },
        )
        normalized = hypotheses["margin_long_to_adv_mcap_quintile_percentile"]["controls"][
            "market_cap_oku"
        ]
        self.assertEqual(normalized, {"normalized_in_axis": True})

    def test_profit_normalization_reports_controls_cycle_flag_and_history_coverage(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for index in range(120):
            ticker = f"5{index:03d}"
            panel.append(
                replace(
                    _panel_row(
                        ticker,
                        per_trailing=5.0 + index,
                        pbr=1.0 + index / 100,
                        market_cap_oku=100.0 + index,
                        er_annual=index / 100,
                        er_reversion_annual=index / 200,
                    ),
                    normalized_per_3fy=1.0 + index,
                    normalized_per_5fy=2.0 + index,
                    eps_cycle_percentile_3fy=1.0 if index >= 114 else 0.5,
                    eps_cycle_peak_3fy=index >= 114,
                    self_range_observed_sessions=1300,
                )
            )
            forwards.append(_forward_row(ticker, -index / 100))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]
        hypotheses = cohort["profit_normalization_hypotheses"]
        aggregate = result["6m"]["aggregate"]["profit_normalization_hypotheses"]

        self.assertGreater(cohort["axes"]["normalized_per_3fy"]["decile_spread_median"], 0)
        self.assertEqual(hypotheses["normalized_per_3fy_coverage"], 1.0)
        self.assertEqual(hypotheses["self_range_coverage"]["at_least_1250"], 1.0)
        cycle = aggregate["cycle_peak_top_er_decile"]
        self.assertEqual(cycle["flagged_n"], 6)
        self.assertEqual(cycle["unflagged_n"], 6)
        self.assertGreater(cycle["mean_median_excess_delta"], 0)

    def test_asset_backed_axis_reports_fixed_interaction_and_controls(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        group_values = (
            (0.5, True, 0.2),
            (0.5, False, 0.0),
            (0.2, True, 0.1),
            (0.2, False, 0.0),
        )
        for group_index, (ratio, return_change, realized) in enumerate(group_values):
            for index in range(25):
                ticker = f"{group_index + 6}{index:03d}"
                panel.append(
                    _panel_row(
                        ticker,
                        per_trailing=10.0 + index / 10,
                        pbr=0.8 + index / 100,
                        market_cap_oku=500.0,
                        net_cash_to_market_cap=ratio - 0.1,
                        investment_securities=5_000_000_000.0,
                        asset_backed_ratio=ratio,
                        equity_ratio=0.5 + index / 1000,
                        shareholder_return_change=return_change,
                    )
                )
                forwards.append(_forward_row(ticker, realized))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]["asset_backed_hypotheses"]
        aggregate = result["6m"]["aggregate"]["asset_backed_hypotheses"]

        self.assertEqual(cohort["asset_backed_ratio_coverage"], 1.0)
        self.assertEqual(set(cohort["controls"]), {"pbr", "market_cap_oku", "equity_ratio"})
        self.assertAlmostEqual(cohort["thick_change_minus_no_change_median_excess"], 0.2)
        self.assertAlmostEqual(cohort["difference_in_differences_median_excess"], 0.1)
        self.assertEqual(aggregate["comparable_cohorts"], 1)
        self.assertEqual(aggregate["difference_in_differences_cohorts"], 1)
        self.assertAlmostEqual(aggregate["mean_difference_in_differences_median_excess"], 0.1)


class MarginSizeNormalizationTest(unittest.TestCase):
    def test_rank_is_tie_aware_within_stable_market_cap_quintiles(self) -> None:
        rows = [
            replace(
                _panel_row(
                    f"{5000 + index}",
                    per_trailing=10.0,
                    market_cap_oku=float(index + 1),
                ),
                margin_long_to_adv=(1.0 if index % 3 < 2 else 2.0),
            )
            for index in range(15)
        ]

        normalized = _with_mcap_quintile_percentiles(rows)

        self.assertEqual(
            [row.margin_long_to_adv_mcap_quintile_percentile for row in normalized[:3]],
            [0.25, 0.25, 1.0],
        )

    def test_missing_and_out_of_population_rows_are_not_normalized(self) -> None:
        missing = replace(_panel_row("6000", per_trailing=10.0), margin_long_to_adv=None)
        excluded = replace(
            _panel_row("6001", per_trailing=10.0),
            in_population=False,
            margin_long_to_adv=2.0,
        )

        normalized = _with_mcap_quintile_percentiles([missing, excluded])

        self.assertTrue(
            all(row.margin_long_to_adv_mcap_quintile_percentile is None for row in normalized)
        )

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

    def test_quality_interaction_compares_er_top_decile_and_applies_all_controls(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(400):
            ticker = f"{4000 + i}"
            in_er_top = i >= 360
            high_quality = 360 <= i < 380
            control = float(i % 20 + 1)
            quality_count = 6 if high_quality else (2 if in_er_top else 4)
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=control,
                    dividend_yield=control / 1_000,
                    er_annual=float(i),
                    market_cap_oku=control * 100,
                    avg_turnover_oku=control,
                    pbr=control / 10,
                    price_change_60d=control / 100,
                    quality_signal_count=quality_count,
                )
            )
            realized = 0.10 if high_quality else (-0.30 if in_er_top else 0.0)
            forwards.append(_forward_row(ticker, realized, horizon="1y"))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["1y"])
        horizon = result["1y"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        interaction = cohorts[0]["quality_interaction"]
        assert isinstance(interaction, dict)
        self.assertEqual(interaction["er_top_decile_n"], 40)
        self.assertEqual(interaction["eligible_n"], 40)
        self.assertEqual(interaction["median_excess_delta"], 0.4)
        self.assertEqual(interaction["trap_rate_delta"], -1.0)
        controls = interaction["controls"]
        assert isinstance(controls, dict)
        for field_name in (
            "market_cap_oku",
            "avg_turnover_oku",
            "pbr",
            "per_trailing",
            "dividend_yield",
            "price_change_60d",
        ):
            with self.subTest(field_name=field_name):
                control_result = controls[field_name]
                assert isinstance(control_result, dict)
                self.assertEqual(control_result["strata_used"], 2)
                self.assertEqual(control_result["matched_weight"], 20)
                self.assertEqual(control_result["stratified_median_excess_delta"], 0.4)
                self.assertEqual(control_result["stratified_trap_rate_delta"], -1.0)
        axes = cohorts[0]["axes"]
        assert isinstance(axes, dict)
        quality_axis = axes["quality_signal_count"]
        assert isinstance(quality_axis, dict)
        self.assertGreater(float(quality_axis["decile_spread_median"]), 0.0)

    def test_shareholder_return_change_compares_low_per_band_and_controls(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(500):
            ticker = f"{5000 + i}"
            in_low_per_band = i < 100
            change = in_low_per_band and i % 2 == 0
            control = float(i % 20 + 1)
            row = _panel_row(
                ticker,
                per_trailing=float(i + 1),
                dividend_yield=control / 1_000,
                market_cap_oku=control * 100,
                avg_turnover_oku=control,
                pbr=control / 10,
                price_change_60d=control / 100,
            )
            if in_low_per_band:
                row = replace(
                    row,
                    dps_streak_up=change,
                    dps_yoy_latest=0.10 if change else 0.0,
                    dps_guidance_up=change,
                    dividend_initiation=change,
                    share_count_reduction_streak=1 if change else 0,
                    shareholder_return_change=change,
                )
            panel.append(row)
            realized = 0.10 if change else (-0.30 if in_low_per_band else 0.0)
            forwards.append(_forward_row(ticker, realized, horizon="1y"))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["1y"])
        horizon = result["1y"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        interaction = cohorts[0]["shareholder_return_change"]
        assert isinstance(interaction, dict)
        self.assertEqual(interaction["low_valuation_n"], 100)
        self.assertEqual(interaction["eligible_n"], 100)
        self.assertEqual(interaction["median_excess_delta"], 0.4)
        self.assertEqual(interaction["trap_rate_delta"], -1.0)
        controls = interaction["controls"]
        assert isinstance(controls, dict)
        for field_name in (
            "dividend_yield",
            "per_trailing",
            "pbr",
            "market_cap_oku",
            "avg_turnover_oku",
            "price_change_60d",
        ):
            with self.subTest(field_name=field_name):
                control_result = controls[field_name]
                assert isinstance(control_result, dict)
                self.assertEqual(control_result["strata_used"], 2)
                self.assertEqual(control_result["matched_weight"], 50)
                self.assertEqual(control_result["stratified_median_excess_delta"], 0.4)
                self.assertEqual(control_result["stratified_trap_rate_delta"], -1.0)
        components = interaction["components"]
        assert isinstance(components, dict)
        for field_name in ("dps_streak_up", "dps_guidance_up", "dividend_initiation"):
            component = components[field_name]
            assert isinstance(component, dict)
            self.assertEqual(component["median_excess_delta"], 0.4)

        aggregate = horizon["aggregate"]
        assert isinstance(aggregate, dict)
        aggregate_interaction = aggregate["shareholder_return_change"]
        assert isinstance(aggregate_interaction, dict)
        self.assertEqual(aggregate_interaction["change_n"], 50)
        self.assertEqual(aggregate_interaction["no_change_n"], 50)
        self.assertEqual(aggregate_interaction["mean_median_excess_delta"], 0.4)

    def test_dislocation_lens_compares_acute_chronic_and_quality_fail_groups(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(500):
            ticker = f"{6000 + i}"
            in_low_per_band = i < 100
            group = i % 10 if in_low_per_band else 9
            acute_quality = in_low_per_band and group == 0
            acute_quality_fail = in_low_per_band and group == 1
            acute = acute_quality or acute_quality_fail
            sector = "サービス業" if i % 2 == 0 else "情報・通信業"
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=float(i + 1),
                    sector_33=sector,
                    market_cap_oku=float((i % 10 + 1) * 100),
                    price_change_60d=(None if i == 3 else (-0.20 if acute else -0.05)),
                    quality_cfo_positive=(None if i == 2 or not in_low_per_band else True),
                    quality_no_dilution=(not acute_quality_fail if in_low_per_band else None),
                    realized_volatility_60d=float(i % 10 + 1) / 100,
                )
            )
            realized = 0.20 if acute_quality else (-0.40 if acute_quality_fail else -0.10)
            forwards.append(_forward_row(ticker, realized, horizon="1y"))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["1y"])
        horizon = result["1y"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        lens = cohorts[0]["dislocation_lens"]
        assert isinstance(lens, dict)
        self.assertEqual(lens["low_valuation_n"], 100)
        self.assertEqual(lens["eligible_n"], 98)
        groups = lens["groups"]
        assert isinstance(groups, dict)
        self.assertEqual(groups["A_acute_quality"]["n"], 10)
        self.assertEqual(groups["B_chronic_quality"]["n"], 78)
        self.assertEqual(groups["C_acute_quality_fail"]["n"], 10)
        self.assertAlmostEqual(lens["A_minus_B"]["median_excess_delta"], 0.3)
        self.assertEqual(lens["A_minus_B"]["trap_rate_delta"], 0.0)
        self.assertAlmostEqual(lens["C_minus_A"]["median_excess_delta"], -0.6)
        self.assertEqual(lens["C_minus_A"]["trap_rate_delta"], 1.0)
        controls = lens["controls"]
        assert isinstance(controls, dict)
        for field_name in (
            "realized_volatility_60d",
            "market_cap_oku",
            "sector_33",
            "per_trailing",
        ):
            with self.subTest(field_name=field_name):
                control = controls[field_name]
                assert isinstance(control, dict)
                self.assertGreater(control["strata_used"], 0)
                self.assertAlmostEqual(control["stratified_median_excess_delta"], 0.3)
                self.assertEqual(control["stratified_trap_rate_delta"], 0.0)

        aggregate = horizon["aggregate"]
        assert isinstance(aggregate, dict)
        aggregate_lens = aggregate["dislocation_lens"]
        assert isinstance(aggregate_lens, dict)
        self.assertEqual(aggregate_lens["group_n"]["A_acute_quality"], 10)
        self.assertAlmostEqual(aggregate_lens["A_minus_B"]["mean_median_excess_delta"], 0.3)
        self.assertEqual(aggregate_lens["C_minus_A"]["mean_trap_rate_delta"], 1.0)

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

    def test_er_level_calibration_compares_absolute_annual_total_return(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for i in range(100):
            ticker = f"37{i:02d}"
            reversion = i / 1000
            panel.append(
                _panel_row(
                    ticker,
                    per_trailing=10.0,
                    er_annual=reversion + 0.02,
                    er_reversion_annual=reversion,
                    er_carry_annual=0.02,
                )
            )
            forwards.append(
                _forward_row(
                    ticker,
                    reversion,
                    total_return=reversion + 0.04,
                    horizon="1y",
                )
            )

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["1y"])

        cohort = result["1y"]["cohorts"][0]
        level = cohort["er_level_calibration"]
        assert isinstance(level, dict)
        self.assertEqual(level["prediction_basis"], "er_annual_absolute")
        self.assertEqual(
            level["realized_basis"],
            "fy_actual_dividend_total_return_annualized_absolute",
        )
        quintiles = level["er_quintiles"]
        assert isinstance(quintiles, list)
        first = quintiles[0]
        self.assertEqual(first["median_predicted_er_annual"], 0.0295)
        self.assertEqual(first["median_realized_total_return_annual"], 0.0495)
        self.assertEqual(first["calibration_error_annual"], 0.02)
        self.assertEqual(first["median_predicted_reversion_annual"], 0.0095)
        self.assertEqual(first["median_predicted_carry_annual"], 0.02)
        self.assertEqual(first["median_realized_price_return_annual"], 0.0095)
        self.assertEqual(first["median_realized_dividend_contribution_annual"], 0.04)
        self.assertEqual(cohort["metric_statuses"]["er_level_calibration"], "eligible")

    def test_er_level_calibration_does_not_treat_missing_total_return_as_zero(self) -> None:
        panel = [
            _panel_row(
                f"38{i:02d}",
                per_trailing=10.0,
                er_annual=0.05,
                er_reversion_annual=0.02,
                er_carry_annual=0.03,
            )
            for i in range(100)
        ]
        forwards = [_forward_row(f"38{i:02d}", 0.10, horizon="1y") for i in range(100)]

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["1y"])

        cohort = result["1y"]["cohorts"][0]
        self.assertEqual(cohort["er_level_calibration"], {})
        self.assertEqual(cohort["metric_statuses"]["er_level_calibration"], "unresolved")

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
        self, extra: list[tuple[ForwardReturnRow, float | None]]
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
        # panel の close が「asof に価格が付いていたか」の権威なので、分類の意図は
        # forward row ではなく panel 側の close で表す。
        for row, panel_close in extra:
            panel.append(_panel_row(row.ticker, per_trailing=None, close=panel_close))
            forwards.append(row)
        return panel, forwards

    def _reason_counts(
        self,
        panel: list[PanelRow],
        forwards: list[ForwardReturnRow],
        *,
        priced_master_without_universe_count: int = 0,
    ) -> tuple[dict[str, int], dict[str, object]]:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_panel(
                root,
                date(2025, 6, 30),
                tuple(panel),
                _panel_diagnostics(
                    master_snapshot_status="exact_date",
                    priced_master_without_universe_count=priced_master_without_universe_count,
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

    def test_unpriced_entry_is_disclosed_without_blocking_the_cohort(self) -> None:
        # panel も価格を持たない = asof に市場に無かった銘柄。除外は正しいので
        # blocker にはならず件数だけ残る。
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
        counts, coverage = self._reason_counts(*self._long_horizon_cohort([(not_listed, None)]))

        self.assertEqual(coverage["entry_not_listed_count"], 1)
        self.assertEqual(coverage["entry_price_gap_count"], 0)
        self.assertEqual(coverage["unpriced_exit_count"], 0)
        self.assertEqual([key for key in counts if key.startswith("entry_price_gap")], [])
        self.assertEqual([key for key in counts if key.startswith("unpriced_exit")], [])

    def test_entry_missing_for_a_priced_name_blocks_even_without_an_entry_date(self) -> None:
        # panel が価格を持つのに forward が entry を持たない銘柄は、取引可能名を
        # 無言で落とす経路である。entry_date が空でも未上場として扱わない。
        dropped = ForwardReturnRow(
            asof="2025-06-30",
            ticker="9400",
            horizon="3y",
            target_date="2028-06-30",
            resolved=False,
            price_return=None,
            stale_price=False,
            entry_date=None,
            exit_date=None,
            status="unresolved_missing_entry",
        )
        counts, coverage = self._reason_counts(*self._long_horizon_cohort([(dropped, 1200.0)]))

        self.assertEqual(coverage["entry_not_listed_count"], 0)
        self.assertEqual(coverage["entry_price_gap_count"], 1)
        self.assertEqual(counts.get("entry_price_gap"), 1)

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
            *self._long_horizon_cohort([(price_gap, 900.0), (unpriced_exit, 1100.0)])
        )

        self.assertEqual(coverage["entry_price_gap_count"], 1)
        self.assertEqual(coverage["unpriced_exit_count"], 1)
        self.assertEqual(counts.get("entry_price_gap"), 1)
        self.assertEqual(coverage["unclassified_unresolved_count"], 0)
        # A name that left the market is counted on its own, but it blocks only when
        # giving it a value changes what the cohort concludes.
        self.assertIsNone(counts.get("unpriced_exit"))

    def test_priced_master_row_count_mismatch_blocks_fail_closed(self) -> None:
        panel, forwards = self._long_horizon_cohort([])
        panel[0] = replace(panel[0], population_coverage_status="priced_master_without_universe")

        counts, coverage = self._reason_counts(
            panel,
            forwards,
            priced_master_without_universe_count=0,
        )

        sensitivity = coverage["priced_master_without_universe"]
        assert isinstance(sensitivity, dict)
        self.assertEqual(sensitivity["excluded_count"], 1)
        self.assertEqual(counts.get("priced_master_without_universe_unmeasured"), 1)

    def test_priced_master_row_without_return_has_a_distinct_blocker(self) -> None:
        missing = ForwardReturnRow(
            asof="2025-06-30",
            ticker="9400",
            horizon="3y",
            target_date="2028-06-30",
            resolved=False,
            price_return=None,
            stale_price=False,
            entry_date="2025-06-30",
            exit_date=None,
            status="unresolved_future_horizon",
        )
        panel, forwards = self._long_horizon_cohort([(missing, None)])
        panel[-1] = replace(panel[-1], population_coverage_status="priced_master_without_universe")

        counts, _ = self._reason_counts(
            panel,
            forwards,
            priced_master_without_universe_count=1,
        )

        self.assertEqual(counts.get("priced_master_without_universe_return_unresolved"), 1)

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


class DelistingExclusionSensitivityTests(unittest.TestCase):
    """Whether the names that left the market could have produced the conclusions."""

    @staticmethod
    def _cohort(
        *, delisted_ranks: tuple[int, ...]
    ) -> tuple[list[PanelRow], list[ForwardReturnRow]]:
        # A cohort where the recommended names beat the rest, plus names that left
        # the market inside the window and so carry no exit value.
        panel = [
            _panel_row(f"{9000 + index}", per_trailing=10.0) for index in range(MIN_AXIS_SAMPLE)
        ]
        forwards = [_forward_row(row.ticker, 0.0) for row in panel]
        for rank in (1, 2):
            panel.append(_panel_row(f"81{rank:02d}", per_trailing=10.0, rank=rank))
            forwards.append(_forward_row(f"81{rank:02d}", 0.30))
        for rank in delisted_ranks:
            ticker = f"82{rank:02d}"
            panel.append(_panel_row(ticker, per_trailing=10.0, rank=rank))
            forwards.append(
                ForwardReturnRow(
                    asof="2025-06-30",
                    ticker=ticker,
                    horizon="6m",
                    target_date="2025-12-29",
                    resolved=False,
                    price_return=None,
                    stale_price=False,
                    entry_date="2025-06-30",
                    exit_date=None,
                    status="unresolved_missing_exit",
                )
            )
        return panel, forwards

    def _sensitivity(self, *, delisted_ranks: tuple[int, ...]) -> dict[str, object]:
        panel, forwards = self._cohort(delisted_ranks=delisted_ranks)
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        cohort = result["6m"]["cohorts"][0]  # type: ignore[index]
        return cohort["coverage"]["delisting_exclusion"]  # type: ignore[index]

    def test_a_conclusion_that_survives_both_ends_is_not_produced_by_the_exclusion(self) -> None:
        # The delisted name is not among the recommendations, so the recommended
        # group stays ahead whichever value the delisting is given.
        panel, forwards = self._cohort(delisted_ranks=())
        panel.append(_panel_row("8300", per_trailing=10.0))
        forwards.append(
            ForwardReturnRow(
                asof="2025-06-30",
                ticker="8300",
                horizon="6m",
                target_date="2025-12-29",
                resolved=False,
                price_return=None,
                stale_price=False,
                entry_date="2025-06-30",
                exit_date=None,
                status="unresolved_missing_exit",
            )
        )
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        sensitivity = result["6m"]["cohorts"][0]["coverage"]["delisting_exclusion"]  # type: ignore[index]

        self.assertEqual(sensitivity["excluded_count"], 1)
        self.assertTrue(sensitivity["direction_stable"])

    def test_a_conclusion_that_moves_between_the_ends_blocks_the_cohort(self) -> None:
        # Half the recommended group left the market. Called total losses they drag
        # the group below the rest of the cohort; called neutral they do not. The
        # cohort cannot say which happened, so it does not get to conclude.
        sensitivity = self._sensitivity(delisted_ranks=(3, 4))

        self.assertEqual(sensitivity["excluded_count"], 2)
        self.assertFalse(sensitivity["direction_stable"])
        imputations = sensitivity["imputations"]
        self.assertLess(imputations["total_loss"]["recommended_rank_top5"], 0)
        self.assertGreater(imputations["neutral"]["recommended_rank_top5"], 0)

    def test_margin_gate_trap_regression_blocks_optional_authority(self) -> None:
        passing = {
            "median_excess_delta": -0.005,
            "trap_rate_delta": 0.0,
        }
        trap_regression = {
            "median_excess_delta": -0.005,
            "trap_rate_delta": 0.01,
        }
        self.assertEqual(_margin_deadline_gate_adoption_sign(passing), 1.0)
        self.assertEqual(_margin_deadline_gate_adoption_sign(trap_regression), 0.0)

        as_reported = {
            "recommended_rank_top5": None,
            "recommended_rank_top10": None,
            "er_calibration": None,
            "margin_deadline_gate_top10": 1.0,
        }
        imputed = {
            "total_loss": as_reported,
            "neutral": {**as_reported, "margin_deadline_gate_top10": 0.0},
        }
        stability = _metric_direction_stability(as_reported, imputed)
        self.assertFalse(stability["margin_deadline_gate_top10"])

    def test_a_conclusion_only_the_survivors_support_blocks_the_cohort(self) -> None:
        # Three of the five recommended names left the market. What the cohort
        # reports is the two survivors' lead; both imputations agree the group did
        # not lead. Comparing the two imputations to each other alone would call
        # that stable, which is the survivorship case the rule exists to catch.
        sensitivity = self._sensitivity(delisted_ranks=(3, 4, 5))

        self.assertEqual(sensitivity["excluded_count"], 3)
        self.assertGreater(sensitivity["as_reported"]["recommended_rank_top5"], 0)
        imputations = sensitivity["imputations"]
        self.assertLess(imputations["total_loss"]["recommended_rank_top5"], 0)
        self.assertEqual(imputations["neutral"]["recommended_rank_top5"], 0.0)
        self.assertFalse(sensitivity["direction_stable"])

    def test_a_cohort_without_delistings_needs_no_imputation(self) -> None:
        sensitivity = self._sensitivity(delisted_ranks=())

        self.assertEqual(sensitivity["excluded_count"], 0)
        self.assertTrue(sensitivity["direction_stable"])
        self.assertEqual(sensitivity["as_reported"], {})
        self.assertEqual(sensitivity["imputations"], {})


class PricedMasterWithoutUniverseSensitivityTests(unittest.TestCase):
    def test_a_direction_split_keeps_the_cohort_blocked(self) -> None:
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        for index in range(100):
            ticker = f"6{index:03d}"
            panel.append(_panel_row(ticker, per_trailing=10.0))
            forwards.append(_forward_row(ticker, index / 100))
        for index in range(5):
            ticker = f"7{index:03d}"
            panel.append(_panel_row(ticker, per_trailing=10.0, rank=index + 1))
            forwards.append(_forward_row(ticker, 0.52))
        for index in range(2):
            ticker = f"8{index:03d}"
            panel.append(
                replace(
                    _panel_row(ticker, per_trailing=None),
                    population_coverage_status="priced_master_without_universe",
                )
            )
            forwards.append(_forward_row(ticker, 1.0))

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        sensitivity = result["6m"]["cohorts"][0]["coverage"][  # type: ignore[index]
            "priced_master_without_universe"
        ]

        self.assertEqual(sensitivity["excluded_count"], 2)
        self.assertEqual(sensitivity["resolved_target_count"], 2)
        self.assertTrue(sensitivity["resolution_complete"])
        self.assertEqual(sensitivity["as_reported"]["recommended_rank_top5"], 0.0)
        self.assertGreater(sensitivity["imputations"]["total_loss"]["recommended_rank_top5"], 0)
        self.assertFalse(sensitivity["direction_stable"])

    def test_a_target_without_an_observed_return_is_not_stable(self) -> None:
        panel = [
            _panel_row(
                f"6{index:03d}",
                per_trailing=10.0,
                rank=index + 1 if index < 10 else None,
                er_reversion_annual=index / 1000,
            )
            for index in range(MIN_AXIS_SAMPLE)
        ]
        missing = replace(
            _panel_row("8999", per_trailing=None),
            population_coverage_status="priced_master_without_universe",
        )
        panel.append(missing)
        forwards = [_forward_row(row.ticker, 0.1) for row in panel[:-1]]
        forwards.append(
            ForwardReturnRow(
                asof="2025-06-30",
                ticker="8999",
                horizon="6m",
                target_date="2025-12-29",
                resolved=False,
                price_return=None,
                stale_price=False,
                entry_date="2025-06-30",
                exit_date=None,
                status="unresolved_future_horizon",
            )
        )

        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        sensitivity = result["6m"]["cohorts"][0]["coverage"][  # type: ignore[index]
            "priced_master_without_universe"
        ]

        self.assertEqual(sensitivity["resolved_target_count"], 0)
        self.assertFalse(sensitivity["resolution_complete"])
        self.assertFalse(sensitivity["direction_stable"])


class CrowdedValueInteractionTest(unittest.TestCase):
    """The direct test of a value trap made of positioning, not of fundamentals."""

    @staticmethod
    def _cohort(*, crowded_return: float) -> tuple[list[PanelRow], list[ForwardReturnRow]]:
        # A cohort where the cheap end splits evenly by margin crowding. Only the
        # crowded half's forward return varies between the two cases.
        panel: list[PanelRow] = []
        forwards: list[ForwardReturnRow] = []
        total = MIN_AXIS_SAMPLE + 20
        decile = total // DECILES
        for index in range(total):
            ticker = f"{3000 + index}"
            cheap = index >= total - decile
            crowded = index >= total - decile // 2
            row = replace(
                _panel_row(ticker, per_trailing=10.0, er_annual=0.5 if cheap else 0.01),
                margin_long_to_adv=20.0 if crowded else 0.5,
            )
            panel.append(row)
            forwards.append(_forward_row(ticker, crowded_return if crowded else 0.0))
        return panel, forwards

    def _crowded_value(self, *, crowded_return: float) -> dict[str, object]:
        panel, forwards = self._cohort(crowded_return=crowded_return)
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        return result["6m"]["cohorts"][0]["crowded_value"]  # type: ignore[index,return-value]

    def test_a_crowded_cheap_half_that_lags_is_reported_as_a_negative_difference(self) -> None:
        crowded_value = self._crowded_value(crowded_return=-0.20)

        self.assertEqual(crowded_value["crowded"]["n"], 6)
        self.assertEqual(crowded_value["uncrowded"]["n"], 6)
        difference = crowded_value["crowded_minus_uncrowded"]
        assert isinstance(difference, float)
        self.assertLess(difference, 0.0)

    def test_a_crowded_cheap_half_that_leads_is_reported_as_a_positive_difference(self) -> None:
        # The measurement must be able to reject the hypothesis, not only confirm
        # it: the same code path has to report the opposite sign when the data says
        # so, or a favourable result would be an artifact of the implementation.
        crowded_value = self._crowded_value(crowded_return=0.20)

        difference = crowded_value["crowded_minus_uncrowded"]
        assert isinstance(difference, float)
        self.assertGreater(difference, 0.0)

    def test_a_cohort_without_margin_data_reports_nothing(self) -> None:
        panel, forwards = self._cohort(crowded_return=-0.20)
        blank = [replace(row, margin_long_to_adv=None) for row in panel]
        result = evaluate_cohorts({"2025-06-30": blank}, {"2025-06-30": forwards}, horizons=["6m"])

        self.assertEqual(result["6m"]["cohorts"][0]["crowded_value"], {})  # type: ignore[index]
