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
    PLAYBOOK_CASH_RICH,
    PLAYBOOK_CASHFLOW_YIELD,
    PLAYBOOK_FCF_YIELD,
    PLAYBOOK_SALES_DISCOUNT,
    PLAYBOOK_STRICT_NET_CASH,
    PLAYBOOK_VALUATION_REVERSION,
    REASON_PRICE_SIGMA,
    REASON_SECTOR_ROTATION,
    REASON_SECTOR_SELF_RANGE,
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
        edinet_ocf_ttm=100.0,
        cfo_yoy=0.1,
        equity_ratio=0.5,
        debt=50.0,
        cash=20.0,
        net_cash=40.0,
        net_cash_to_market_cap=0.2,
        fcf_ttm=80.0,
        fcf_yield=0.05,
        capex_ttm=20.0,
        ebitda_ttm=120.0,
        consolidation_basis="consolidated",
        ttm_quality_ev_ebitda=TTMQuality.EXACT,
        ttm_quality_p_s=TTMQuality.EXACT,
        ttm_quality_pcfr=TTMQuality.EXACT,
        ttm_quality_ocf_yield=TTMQuality.EXACT,
        ttm_quality_sales=TTMQuality.EXACT,
        ttm_quality_fcf_yield=TTMQuality.EXACT,
        ttm_quality_net_cash=TTMQuality.EXACT,
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
        self.assertEqual(result.evidence_hits[0].name, PLAYBOOK_VALUATION_REVERSION)
        self.assertIn(REASON_SECTOR_SELF_RANGE, result.evidence_hits[0].reasons)

    def test_short_history_skips_condition_a(self) -> None:
        result = evaluate_screening(_financial(), _derived(short_history_flag=True), RULES)
        valuation = next(
            evidence_hit
            for evidence_hit in result.evidence_hits
            if evidence_hit.name == PLAYBOOK_VALUATION_REVERSION
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
        self.assertIn(REASON_PRICE_SIGMA, result.evidence_hits[0].reasons)

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
        self.assertIn(REASON_PRICE_SIGMA, result.evidence_hits[0].reasons)

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
        self.assertIn(REASON_SECTOR_ROTATION, result.evidence_hits[0].reasons)

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
        self.assertIn(REASON_SECTOR_SELF_RANGE, result.evidence_hits[0].reasons)

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

    def test_negative_ev_ebitda_is_not_a_valuation_discount(self) -> None:
        result = evaluate_screening(
            _financial(
                per_trailing=None,
                pbr=None,
                ev_ebitda=-26.0,
                ebitda_ttm=-10.0,
                ttm_quality_ev_ebitda=TTMQuality.EXACT,
            ),
            _derived(
                sector_median_gap={"ev_ebitda": -0.9},
                self_range_percentile={"ev_ebitda": 0.01},
                sigma_gap={"ev_ebitda": -2.0},
                price_change_60d=-0.2,
                sector_relative_strength_percentile=0.8,
                ticker_return_4w=0.0,
                sector_return_4w=0.0,
            ),
            RULES,
        )
        self.assertFalse(result.pass_fail)
        self.assertIn("valuation_reversion_condition_a_no_metric", result.null_reasons)

    def test_cash_rich_asset_discount_hits(self) -> None:
        result = evaluate_screening(
            _financial(cash_to_market_cap=0.45, price_to_equity=0.8, operating_profit=10.0),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertIn(
            PLAYBOOK_CASH_RICH, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )

    def test_cash_rich_asset_discount_rejects_low_equity_ratio(self) -> None:
        result = evaluate_screening(
            _financial(
                cash_to_market_cap=0.8,
                price_to_equity=0.8,
                equity_ratio=0.2,
                operating_profit=10.0,
            ),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertNotIn(
            PLAYBOOK_CASH_RICH, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )

    def test_cash_rich_asset_discount_rejects_edinet_net_debt_contradiction(self) -> None:
        result = evaluate_screening(
            _financial(
                cash_to_market_cap=0.8,
                price_to_equity=0.8,
                equity_ratio=0.5,
                operating_profit=10.0,
                net_cash_to_market_cap=-0.1,
            ),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertNotIn(
            PLAYBOOK_CASH_RICH, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )
        self.assertIn("cash_rich_edinet_net_cash_contradiction", result.null_reasons)

    def test_cashflow_yield_discount_hits(self) -> None:
        result = evaluate_screening(
            _financial(ocf_ttm=100.0, ocf_yield=0.1),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertIn(
            PLAYBOOK_CASHFLOW_YIELD, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )

    def test_strict_net_cash_discount_hits_with_edinet_debt(self) -> None:
        result = evaluate_screening(
            _financial(
                net_cash=120.0,
                net_cash_to_market_cap=0.35,
                price_to_equity=0.8,
                equity_ratio=0.5,
                operating_profit=10.0,
            ),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertIn(
            PLAYBOOK_STRICT_NET_CASH, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )

    def test_strict_net_cash_discount_requires_edinet_quality(self) -> None:
        result = evaluate_screening(
            _financial(
                net_cash=120.0,
                net_cash_to_market_cap=0.35,
                price_to_equity=0.8,
                equity_ratio=0.5,
                operating_profit=10.0,
                ttm_quality_net_cash=TTMQuality.UNAVAILABLE,
            ),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertNotIn(
            PLAYBOOK_STRICT_NET_CASH, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )
        self.assertIn("strict_net_cash_unavailable", result.null_reasons)

    def test_strict_net_cash_discount_rejects_assumed_zero_debt(self) -> None:
        result = evaluate_screening(
            _financial(
                net_cash=120.0,
                net_cash_to_market_cap=0.35,
                price_to_equity=0.8,
                equity_ratio=0.5,
                operating_profit=10.0,
                edinet_failure_reasons="debt_assumed_zero",
            ),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertNotIn(
            PLAYBOOK_STRICT_NET_CASH, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )
        self.assertIn("strict_net_cash_debt_assumed_zero", result.null_reasons)

    def test_fcf_yield_discount_hits(self) -> None:
        result = evaluate_screening(
            _financial(
                ocf_ttm=999.0,
                edinet_ocf_ttm=120.0,
                fcf_ttm=100.0,
                fcf_yield=0.1,
                capex_ttm=20.0,
                cfo_yoy=0.2,
            ),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        evidence_hit = next(
            evidence_hit
            for evidence_hit in result.evidence_hits
            if evidence_hit.name == PLAYBOOK_FCF_YIELD
        )
        self.assertEqual(evidence_hit.metrics["edinet_ocf_ttm"], 120.0)
        self.assertNotIn("ocf_ttm", evidence_hit.metrics)

    def test_fcf_yield_discount_filters_unrelated_edinet_failures(self) -> None:
        result = evaluate_screening(
            _financial(
                edinet_ocf_ttm=120.0,
                fcf_ttm=100.0,
                fcf_yield=0.1,
                capex_ttm=20.0,
                cfo_yoy=0.2,
                edinet_failure_reasons="debt_assumed_zero,non_consolidated_fallback",
            ),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        evidence_hit = next(
            evidence_hit
            for evidence_hit in result.evidence_hits
            if evidence_hit.name == PLAYBOOK_FCF_YIELD
        )
        self.assertEqual(
            evidence_hit.metrics["edinet_failure_reasons"], "non_consolidated_fallback"
        )

    def test_fcf_yield_discount_rejects_missing_edinet_ocf(self) -> None:
        result = evaluate_screening(
            _financial(edinet_ocf_ttm=None, fcf_ttm=100.0, fcf_yield=0.1, cfo_yoy=0.2),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertNotIn(
            PLAYBOOK_FCF_YIELD, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )
        self.assertIn("fcf_yield_missing_edinet_ocf", result.null_reasons)

    def test_fcf_yield_discount_rejects_unavailable_quality(self) -> None:
        result = evaluate_screening(
            _financial(
                fcf_ttm=100.0,
                fcf_yield=0.1,
                cfo_yoy=0.2,
                ttm_quality_fcf_yield=TTMQuality.UNAVAILABLE,
            ),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertNotIn(
            PLAYBOOK_FCF_YIELD, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )
        self.assertIn("fcf_yield_ttm_not_exact", result.null_reasons)

    def test_fcf_yield_discount_rejects_approximated_quality(self) -> None:
        result = evaluate_screening(
            _financial(
                fcf_ttm=100.0,
                fcf_yield=0.1,
                cfo_yoy=0.2,
                ttm_quality_fcf_yield=TTMQuality.APPROXIMATED,
            ),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertNotIn(
            PLAYBOOK_FCF_YIELD, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )
        self.assertIn("fcf_yield_ttm_not_exact", result.null_reasons)

    def test_cashflow_yield_discount_requires_cfo_yoy_when_configured(self) -> None:
        result = evaluate_screening(
            _financial(ocf_ttm=100.0, ocf_yield=0.1, cfo_yoy=None),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertNotIn(
            PLAYBOOK_CASHFLOW_YIELD, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )
        self.assertIn("cashflow_yield_missing_cfo_yoy", result.null_reasons)

    def test_cashflow_yield_discount_rejects_cfo_deterioration(self) -> None:
        result = evaluate_screening(
            _financial(ocf_ttm=100.0, ocf_yield=0.1, cfo_yoy=-0.25),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertNotIn(
            PLAYBOOK_CASHFLOW_YIELD, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )

    def test_financial_sector_is_excluded_from_operating_cashflow_lane(self) -> None:
        result = evaluate_screening(
            _financial(ocf_ttm=100.0, ocf_yield=0.1, cfo_yoy=0.2),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
            sector_33="銀行業",
        )
        self.assertNotIn(
            PLAYBOOK_CASHFLOW_YIELD, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )
        self.assertIn("cashflow_yield_excluded_sector", result.null_reasons)

    def test_utility_sector_is_excluded_from_operating_cashflow_lane(self) -> None:
        result = evaluate_screening(
            _financial(ocf_ttm=100.0, ocf_yield=0.1, cfo_yoy=0.2),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
            sector_33="電気・ガス業",
        )
        self.assertNotIn(
            PLAYBOOK_CASHFLOW_YIELD, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )
        self.assertIn("cashflow_yield_excluded_sector", result.null_reasons)

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
        self.assertIn(
            PLAYBOOK_SALES_DISCOUNT, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )

    def test_financial_sector_is_excluded_from_sales_discount_lane(self) -> None:
        result = evaluate_screening(
            _financial(
                p_s=0.4,
                sales_yoy=0.1,
                operating_profit=10.0,
            ),
            _derived(
                sector_median_gap={"p_s": -0.45},
                self_range_percentile={},
                sigma_gap={},
            ),
            RULES,
            sector_33="銀行業",
        )
        self.assertNotIn(
            PLAYBOOK_SALES_DISCOUNT, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )
        self.assertIn("sales_discount_excluded_sector", result.null_reasons)

    def test_financial_sector_is_excluded_from_cash_rich_lane(self) -> None:
        result = evaluate_screening(
            _financial(cash_to_market_cap=0.45, price_to_equity=0.8, operating_profit=10.0),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
            sector_33="銀行業",
        )
        self.assertNotIn(
            PLAYBOOK_CASH_RICH, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )
        self.assertIn("cash_rich_excluded_sector", result.null_reasons)

    def test_utility_sector_is_excluded_from_cash_rich_lane(self) -> None:
        result = evaluate_screening(
            _financial(cash_to_market_cap=0.45, price_to_equity=0.8, operating_profit=10.0),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
            sector_33="電気・ガス業",
        )
        self.assertNotIn(
            PLAYBOOK_CASH_RICH, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )
        self.assertIn("cash_rich_excluded_sector", result.null_reasons)
