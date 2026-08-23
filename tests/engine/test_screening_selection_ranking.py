"""The evidence strength keys are the secondary ordering, so they are pinned here.

Each key is consumed as part of a cross-ticker sort tuple, ascending. A key that reads a
metric no Evidence Pattern emits falls to its default for every candidate and orders nothing while
still looking like a tiebreaker, which is how one of them went unnoticed. These tests
assert the direction of each surviving element against the metrics its own Evidence Pattern emits.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.rules import (
    EVIDENCE_PATTERN_CASH_RICH,
    EVIDENCE_PATTERN_CASHFLOW_YIELD,
    EVIDENCE_PATTERN_SALES_DISCOUNT,
    EVIDENCE_PATTERN_VALUATION_REVERSION,
)
from baibai_engine.screening.selection.ranking import _evidence_strength_key


class EvidenceStrengthKeyTest(unittest.TestCase):
    def test_cash_rich_orders_the_thicker_cash_pile_first(self) -> None:
        richer = _evidence_strength_key(EVIDENCE_PATTERN_CASH_RICH, {"cash_to_market_cap": 0.8})
        thinner = _evidence_strength_key(EVIDENCE_PATTERN_CASH_RICH, {"cash_to_market_cap": 0.4})
        self.assertLess(richer, thinner)

    def test_every_cash_rich_element_is_fed_by_the_evidence_pattern(self) -> None:
        # The Evidence Pattern's own metric map, so a key element that reads something else
        # would sit at its default here and the ordering above would collapse.
        emitted = {
            "cash_to_market_cap": 0.8,
            "net_cash_to_market_cap": 0.5,
            "debt": 0.0,
            "cash": 1.0,
            "pbr": 0.7,
            "equity_ratio": 0.6,
            "operating_profit": 1.0,
        }
        self.assertNotEqual(
            _evidence_strength_key(EVIDENCE_PATTERN_CASH_RICH, emitted),
            _evidence_strength_key(EVIDENCE_PATTERN_CASH_RICH, {}),
        )

    def test_cashflow_yield_orders_the_higher_yield_and_then_the_stronger_growth(self) -> None:
        higher = _evidence_strength_key(
            EVIDENCE_PATTERN_CASHFLOW_YIELD, {"ocf_yield": 0.2, "cfo_yoy": 0.0}
        )
        lower = _evidence_strength_key(
            EVIDENCE_PATTERN_CASHFLOW_YIELD, {"ocf_yield": 0.1, "cfo_yoy": 0.9}
        )
        self.assertLess(higher, lower)
        growing = _evidence_strength_key(
            EVIDENCE_PATTERN_CASHFLOW_YIELD, {"ocf_yield": 0.2, "cfo_yoy": 0.5}
        )
        shrinking = _evidence_strength_key(
            EVIDENCE_PATTERN_CASHFLOW_YIELD, {"ocf_yield": 0.2, "cfo_yoy": -0.1}
        )
        self.assertLess(growing, shrinking)

    def test_sales_discount_orders_the_deeper_gap_then_growth_then_profitability(self) -> None:
        deeper = _evidence_strength_key(
            EVIDENCE_PATTERN_SALES_DISCOUNT, {"ps_sector_gap": -0.8, "sales_yoy": 0.1}
        )
        shallower = _evidence_strength_key(
            EVIDENCE_PATTERN_SALES_DISCOUNT, {"ps_sector_gap": -0.5, "sales_yoy": 0.9}
        )
        self.assertLess(deeper, shallower)
        profitable = _evidence_strength_key(
            EVIDENCE_PATTERN_SALES_DISCOUNT,
            {"ps_sector_gap": -0.5, "sales_yoy": 0.1, "operating_profit": 1.0},
        )
        loss_making = _evidence_strength_key(
            EVIDENCE_PATTERN_SALES_DISCOUNT,
            {"ps_sector_gap": -0.5, "sales_yoy": 0.1, "operating_profit": -1.0},
        )
        self.assertLess(profitable, loss_making)

    def test_valuation_reversion_orders_the_cheaper_and_more_beaten_down_first(self) -> None:
        cheaper = _evidence_strength_key(
            EVIDENCE_PATTERN_VALUATION_REVERSION,
            {
                "condition_a_sector_median_gap": -0.5,
                "condition_a_self_range_percentile": 0.1,
                "condition_b_sigma_gap": -2.0,
                "price_change_60d": -0.3,
            },
        )
        dearer = _evidence_strength_key(
            EVIDENCE_PATTERN_VALUATION_REVERSION,
            {
                "condition_a_sector_median_gap": -0.2,
                "condition_a_self_range_percentile": 0.9,
                "condition_b_sigma_gap": -0.1,
                "price_change_60d": 0.4,
            },
        )
        self.assertLess(cheaper, dearer)

    def test_a_key_element_that_no_evidence_pattern_emits_would_order_nothing(self) -> None:
        # The shape the removed element had: absent from every metric map, so identical
        # for every candidate. Pinned as a property of the keys rather than of one name.
        for evidence_pattern in (
            EVIDENCE_PATTERN_CASH_RICH,
            EVIDENCE_PATTERN_CASHFLOW_YIELD,
            EVIDENCE_PATTERN_SALES_DISCOUNT,
            EVIDENCE_PATTERN_VALUATION_REVERSION,
        ):
            with self.subTest(evidence_pattern=evidence_pattern):
                self.assertEqual(
                    _evidence_strength_key(
                        evidence_pattern, {"a_metric_no_evidence_pattern_emits": 1.0}
                    ),
                    _evidence_strength_key(evidence_pattern, {}),
                )


if __name__ == "__main__":
    unittest.main()
