from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.rule_config import load_screening_rules
from baibai_loop.screening.rules import (
    REASON_PRICE_SIGMA,
    REASON_SECTOR_ROTATION,
    REASON_SECTOR_SELF_RANGE,
    SIGNAL_CASH_RICH,
    SIGNAL_CASHFLOW_YIELD,
    SIGNAL_SALES_DISCOUNT,
    SIGNAL_VALUATION_REVERSION,
    evaluate_screening,
)
from baibai_loop.screening.schema import DerivedMetrics, FinancialSnapshot, TTMQuality

RULES = load_screening_rules()


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
        ttm_quality_p_s=TTMQuality.EXACT,
        ttm_quality_pcfr=TTMQuality.EXACT,
        ttm_quality_ocf_yield=TTMQuality.EXACT,
        ttm_quality_sales=TTMQuality.EXACT,
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
        result = evaluate_screening(_financial(), _derived(), RULES)
        self.assertTrue(result.pass_fail)
        self.assertEqual(result.signals[0].name, SIGNAL_VALUATION_REVERSION)
        self.assertIn(REASON_SECTOR_SELF_RANGE, result.signals[0].reasons)

    def test_short_history_skips_condition_a(self) -> None:
        result = evaluate_screening(_financial(), _derived(short_history_flag=True), RULES)
        valuation = next(
            signal for signal in result.signals if signal.name == SIGNAL_VALUATION_REVERSION
        )
        self.assertNotIn(REASON_SECTOR_SELF_RANGE, valuation.reasons)
        self.assertIn("valuation_reversion_condition_a_short_history", result.null_reasons)

    def test_condition_b_uses_sigma_and_price_drop(self) -> None:
        result = evaluate_screening(
            _financial(eps_yoy=0.1, sales_yoy=0.1, operating_profit_yoy=0.1),
            _derived(
                sector_median_gap={"per_trailing": 0.0},
                self_range_percentile={"per_trailing": 0.5},
                sigma_gap={"per_trailing": -1.4},
            ),
            RULES,
        )
        self.assertIn(REASON_PRICE_SIGMA, result.signals[0].reasons)

    def test_deterioration_blocks_conditions_b_and_c(self) -> None:
        result = evaluate_screening(
            _financial(eps_yoy=-0.35, sales_yoy=0.1, operating_profit_yoy=0.1),
            _derived(
                sector_median_gap={"per_trailing": 0.0},
                self_range_percentile={"per_trailing": 0.5},
                sigma_gap={"per_trailing": -1.4},
            ),
            RULES,
        )
        self.assertFalse(result.pass_fail)

    def test_missing_yoy_does_not_count_as_deterioration(self) -> None:
        result = evaluate_screening(
            _financial(eps_yoy=None, sales_yoy=None, operating_profit_yoy=None),
            _derived(
                sector_median_gap={"per_trailing": 0.0},
                self_range_percentile={"per_trailing": 0.5},
                sigma_gap={"per_trailing": -1.4},
            ),
            RULES,
        )
        self.assertIn(REASON_PRICE_SIGMA, result.signals[0].reasons)

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
            RULES,
        )
        self.assertIn(REASON_SECTOR_ROTATION, result.signals[0].reasons)

    def test_ev_ebitda_participates_when_ttm_quality_is_exact(self) -> None:
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
            RULES,
        )
        self.assertTrue(result.pass_fail)
        self.assertIn(REASON_SECTOR_SELF_RANGE, result.signals[0].reasons)

    def test_ev_ebitda_is_skipped_when_ttm_quality_is_not_exact(self) -> None:
        result = evaluate_screening(
            _financial(ttm_quality_ev_ebitda=TTMQuality.APPROXIMATED),
            _derived(
                sector_median_gap={"ev_ebitda": -0.4},
                self_range_percentile={"ev_ebitda": 0.1},
                sigma_gap={"ev_ebitda": -1.5},
                price_change_60d=-0.05,
                sector_relative_strength_percentile=0.8,
                ticker_return_4w=0.0,
                sector_return_4w=0.0,
            ),
            RULES,
        )
        self.assertFalse(result.pass_fail)

    def test_cash_rich_asset_discount_hits(self) -> None:
        result = evaluate_screening(
            _financial(cash_to_market_cap=0.45, price_to_equity=0.8, operating_profit=10.0),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertIn(SIGNAL_CASH_RICH, [signal.name for signal in result.signals])

    def test_cashflow_yield_discount_hits(self) -> None:
        result = evaluate_screening(
            _financial(ocf_ttm=100.0, ocf_yield=0.1),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertIn(SIGNAL_CASHFLOW_YIELD, [signal.name for signal in result.signals])

    def test_sales_discount_growth_allows_op_loss_with_cfo_positive(self) -> None:
        result = evaluate_screening(
            _financial(
                p_s=0.4,
                sales_yoy=0.1,
                operating_profit=-10.0,
                ocf_ttm=20.0,
            ),
            _derived(
                sector_median_gap={"p_s": -0.45},
                self_range_percentile={},
                sigma_gap={},
            ),
            RULES,
        )
        self.assertIn(SIGNAL_SALES_DISCOUNT, [signal.name for signal in result.signals])
