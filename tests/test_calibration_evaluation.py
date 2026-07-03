from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.calibration.evaluation import (
    _spearman,
    evaluate_cohorts,
)
from baibai_loop.screening.calibration.forward import ForwardReturnRow
from baibai_loop.screening.calibration.panel import PanelRow


def _panel_row(ticker: str, *, per_trailing: float | None, rank: int | None = None) -> PanelRow:
    return PanelRow(
        asof="2025-06-30",
        ticker=ticker,
        sector_33="サービス業",
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

    def test_cohort_skipped_when_population_too_small(self) -> None:
        panel = [_panel_row("1000", per_trailing=10.0)]
        forwards = [_forward_row("1000", 0.1)]
        result = evaluate_cohorts({"2025-06-30": panel}, {"2025-06-30": forwards}, horizons=["6m"])
        horizon = result["6m"]
        assert isinstance(horizon, dict)
        cohorts = horizon["cohorts"]
        assert isinstance(cohorts, list)
        self.assertEqual(cohorts, [])


if __name__ == "__main__":
    unittest.main()
