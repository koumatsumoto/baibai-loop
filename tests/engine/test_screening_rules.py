from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.rules import (
    PLAYBOOK_CASH_RICH,
    PLAYBOOK_CASHFLOW_YIELD,
    PLAYBOOK_SALES_DISCOUNT,
    PLAYBOOK_VALUATION_REVERSION,
    REASON_SECTOR_SELF_RANGE,
    REASON_VALUATION_SIGMA,
    evaluate_screening,
)
from baibai_engine.screening.schema import DerivedMetrics, FinancialSnapshot, TTMQuality

RULES = load_screening_rules()


def _financial(**overrides: object) -> FinancialSnapshot:
    base = dict(
        latest_disclosed_at=None,
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
    def test_removed_output_selection_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rules.yaml"
            payload = RULES.model_dump(mode="json")
            payload["output"]["removed_output_field"] = True
            path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

            with self.assertRaises(ValueError):
                load_screening_rules(path)

    def test_unknown_default_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rules.yaml"
            payload = RULES.model_dump(mode="json")
            payload["selection"]["default_profile"] = "balnaced"
            path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unknown default selection profile"):
                load_screening_rules(path)

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

    def test_condition_b_hits_on_sigma_gap(self) -> None:
        result = evaluate_screening(
            _financial(eps_yoy=0.1, sales_yoy=0.1, operating_profit_yoy=0.1),
            _derived(
                sector_median_gap={"per_trailing": 0.0},
                self_range_percentile={"per_trailing": 0.5},
                sigma_gap={"per_trailing": -1.4},
            ),
            RULES,
        )
        self.assertIn(REASON_VALUATION_SIGMA, result.evidence_hits[0].reasons)

    def test_condition_b_hits_when_price_change_60d_missing(self) -> None:
        # 60 日下落は要件ではない。price_change_60d が欠損でも σギャップ充足 &
        # 悪化ゲート非該当なら条件 B は hit し、metrics に None を事実記録する。
        result = evaluate_screening(
            _financial(eps_yoy=0.1, sales_yoy=0.1, operating_profit_yoy=0.1),
            _derived(
                sector_median_gap={"per_trailing": 0.0},
                self_range_percentile={"per_trailing": 0.5},
                sigma_gap={"per_trailing": -1.4},
                price_change_60d=None,
            ),
            RULES,
        )
        valuation = next(
            evidence_hit
            for evidence_hit in result.evidence_hits
            if evidence_hit.name == PLAYBOOK_VALUATION_REVERSION
        )
        self.assertIn(REASON_VALUATION_SIGMA, valuation.reasons)
        self.assertIsNone(valuation.metrics["price_change_60d"])

    def test_condition_b_hits_when_price_not_down_60d(self) -> None:
        # 60 日で下落していない (price_change_60d 正) 銘柄でも σギャップ充足なら hit。
        result = evaluate_screening(
            _financial(eps_yoy=0.1, sales_yoy=0.1, operating_profit_yoy=0.1),
            _derived(
                sector_median_gap={"per_trailing": 0.0},
                self_range_percentile={"per_trailing": 0.5},
                sigma_gap={"per_trailing": -1.4},
                price_change_60d=0.08,
            ),
            RULES,
        )
        self.assertIn(REASON_VALUATION_SIGMA, result.evidence_hits[0].reasons)

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
        self.assertIn(REASON_VALUATION_SIGMA, result.evidence_hits[0].reasons)

    def test_sector_rotation_alone_does_not_hit(self) -> None:
        # 相対モメンタム (sector 内劣後) だけでは valuation 条件を満たさないため
        # evidence hit にならない (割安の判定軸は valuation と耐性のみ)。
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
        self.assertFalse([hit for hit in result.evidence_hits if hit.name == "valuation-reversion"])

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

    def test_cashflow_yield_discount_hits(self) -> None:
        result = evaluate_screening(
            _financial(ocf_ttm=100.0, ocf_yield=0.1),
            _derived(sector_median_gap={}, self_range_percentile={}, sigma_gap={}),
            RULES,
        )
        self.assertIn(
            PLAYBOOK_CASHFLOW_YIELD, [evidence_hit.name for evidence_hit in result.evidence_hits]
        )

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

    def test_financial_sector_is_excluded_from_operating_cashflow_playbook(self) -> None:
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

    def test_utility_sector_is_excluded_from_operating_cashflow_playbook(self) -> None:
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

    def test_financial_sector_is_excluded_from_sales_discount_playbook(self) -> None:
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

    def test_financial_sector_is_excluded_from_valuation_reversion_playbook(self) -> None:
        result = evaluate_screening(
            _financial(),
            _derived(),
            RULES,
            sector_33="銀行業",
        )
        self.assertNotIn(
            PLAYBOOK_VALUATION_REVERSION,
            [evidence_hit.name for evidence_hit in result.evidence_hits],
        )
        self.assertIn("valuation_reversion_excluded_sector", result.null_reasons)

    def test_utility_sector_stays_eligible_for_valuation_reversion_playbook(self) -> None:
        result = evaluate_screening(
            _financial(),
            _derived(),
            RULES,
            sector_33="電気・ガス業",
        )
        self.assertIn(
            PLAYBOOK_VALUATION_REVERSION,
            [evidence_hit.name for evidence_hit in result.evidence_hits],
        )

    def test_cash_rich_asset_discount_hits(self) -> None:
        result = evaluate_screening(
            _financial(cash_to_market_cap=0.45, pbr=0.8, operating_profit=10.0),
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
                pbr=0.8,
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
                pbr=0.8,
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
