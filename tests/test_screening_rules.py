from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.rules import THRESHOLD_A, THRESHOLD_B, THRESHOLD_C, evaluate_screening
from baibai_loop.screening.schema import DerivedMetrics, FinancialSnapshot, TTMQuality


def _financial(**overrides: object) -> FinancialSnapshot:
    base = dict(
        per_forward=8.0,
        per_trailing=9.0,
        pbr=0.8,
        ev_ebitda=4.0,
        p_s=0.6,
        pcfr=5.0,
        eps=100.0,
        sales_ttm=1000.0,
        ocf_ttm=100.0,
        debt=50.0,
        cash=20.0,
        ebitda_ttm=120.0,
        consolidation_basis="consolidated",
        ttm_quality_ev_ebitda=TTMQuality.EXACT,
        ttm_quality_p_s=TTMQuality.APPROXIMATED,
        ttm_quality_pcfr=TTMQuality.UNAVAILABLE,
    )
    base.update(overrides)
    return FinancialSnapshot(**base)


def _derived(**overrides: object) -> DerivedMetrics:
    base = dict(
        price_change_60d=-0.2,
        sector_relative_strength_4w=-0.1,
        sector_median_gap={"per_trailing": -0.25, "pbr": -0.1, "ev_ebitda": -0.1},
        self_range_percentile={"per_trailing": 0.15, "pbr": 0.4, "ev_ebitda": 0.4},
        sigma_gap={"per_trailing": -1.2, "pbr": -0.2, "ev_ebitda": -0.4},
        sector_relative_strength_percentile=0.15,
        ticker_return_4w=-0.12,
        sector_return_4w=-0.05,
        short_history_flag=False,
    )
    base.update(overrides)
    return DerivedMetrics(**base)


class ScreeningRulesTests(unittest.TestCase):
    def test_condition_a_hits_when_sector_gap_and_self_range_match(self) -> None:
        result = evaluate_screening(_financial(), _derived())
        self.assertTrue(result.pass_fail)
        self.assertIn(THRESHOLD_A, result.threshold_hit)

    def test_short_history_skips_condition_a(self) -> None:
        result = evaluate_screening(_financial(), _derived(short_history_flag=True))
        self.assertNotIn(THRESHOLD_A, result.threshold_hit)
        self.assertIn("condition_a_short_history", result.null_reasons)

    def test_condition_b_uses_sigma_and_price_drop(self) -> None:
        result = evaluate_screening(
            _financial(eps_yoy=0.1, sales_yoy=0.1, operating_profit_yoy=0.1),
            _derived(
                sector_median_gap={"per_trailing": 0.0},
                self_range_percentile={"per_trailing": 0.5},
                sigma_gap={"per_trailing": -1.4},
            ),
        )
        self.assertIn(THRESHOLD_B, result.threshold_hit)

    def test_deterioration_blocks_conditions_b_and_c(self) -> None:
        result = evaluate_screening(
            _financial(eps_yoy=-0.35, sales_yoy=0.1, operating_profit_yoy=0.1),
            _derived(
                sector_median_gap={"per_trailing": 0.0},
                self_range_percentile={"per_trailing": 0.5},
                sigma_gap={"per_trailing": -1.4},
            ),
        )
        self.assertNotIn(THRESHOLD_B, result.threshold_hit)
        self.assertNotIn(THRESHOLD_C, result.threshold_hit)

    def test_missing_yoy_does_not_count_as_deterioration(self) -> None:
        result = evaluate_screening(
            _financial(eps_yoy=None, sales_yoy=None, operating_profit_yoy=None),
            _derived(
                sector_median_gap={"per_trailing": 0.0},
                self_range_percentile={"per_trailing": 0.5},
                sigma_gap={"per_trailing": -1.4},
            ),
        )
        self.assertIn(THRESHOLD_B, result.threshold_hit)

    def test_condition_c_hits_on_sector_rotation(self) -> None:
        result = evaluate_screening(
            _financial(eps_yoy=0.1, sales_yoy=0.1, operating_profit_yoy=0.1),
            _derived(
                sector_median_gap={"per_trailing": 0.0},
                self_range_percentile={"per_trailing": 0.5},
                sigma_gap={"per_trailing": -0.2},
                price_change_60d=-0.05,
                sector_relative_strength_percentile=0.2,
                ticker_return_4w=-0.1,
                sector_return_4w=-0.04,
            ),
        )
        self.assertIn(THRESHOLD_C, result.threshold_hit)

    def test_ev_ebitda_is_skipped_pending_issue_15(self) -> None:
        # issue #15: _valuation_history の ev_ebitda 近似が price / snapshot.ev_ebitda
        # に縮退しているため、ttm_quality_ev_ebitda == EXACT であっても閾値 A/B の
        # rule metrics から除外する。ev_ebitda だけを hit させても pass しない。
        result = evaluate_screening(
            _financial(ttm_quality_ev_ebitda=TTMQuality.EXACT),
            _derived(
                sector_median_gap={"ev_ebitda": -0.4},
                self_range_percentile={"ev_ebitda": 0.1},
                sigma_gap={"ev_ebitda": -1.5},
                price_change_60d=-0.05,
                sector_relative_strength_percentile=0.8,
                ticker_return_4w=0.0,
                sector_return_4w=0.0,
            ),
        )
        self.assertFalse(result.pass_fail)
