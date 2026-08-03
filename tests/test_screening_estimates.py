from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_engine.screening.estimates import (
    BUYBACK_CLIP,
    REALIZATION_RATE_ANNUAL,
    UPSIDE_CAP,
    estimate_expected_return,
)
from baibai_engine.screening.schema import DerivedMetrics, FinancialSnapshot


def _financial(
    *,
    pbr: float | None = 0.8,
    per_forward: float | None = 10.0,
    per_trailing: float | None = None,
    dividend_yield: float | None = 0.03,
    net_share_change_yoy: float | None = -0.02,
    market_cap: float | None = 1e10,
    shares_outstanding: float | None = 1e8,
) -> FinancialSnapshot:
    return FinancialSnapshot(
        latest_disclosed_at=None,
        per_forward=per_forward,
        per_trailing=per_trailing,
        pbr=pbr,
        ev_ebitda=None,
        p_s=None,
        pcfr=None,
        eps=None,
        sales_ttm=None,
        ocf_ttm=None,
        dividend_yield=dividend_yield,
        net_share_change_yoy=net_share_change_yoy,
        market_cap=market_cap,
        shares_outstanding=shares_outstanding,
    )


def _derived(
    *,
    sector_median_value: dict[str, float | None] | None = None,
    self_range_median: dict[str, float | None] | None = None,
) -> DerivedMetrics:
    return DerivedMetrics(
        sector_median_value=sector_median_value or {},
        self_range_median=self_range_median or {},
    )


class EstimateExpectedReturnTest(unittest.TestCase):
    def test_blends_asset_and_earnings_anchor_with_conservative_min(self) -> None:
        # pbr: current 0.8, sector median 1.2, 自己中央値 1.0 → 保守 anchor 1.0 → +25%
        # per_forward: current 10, sector median 15 (自己欠損) → +50%
        # blend = (0.25 + 0.50) / 2 = 0.375 (cap 未満)
        derived = _derived(
            sector_median_value={"pbr": 1.2, "per_forward": 15.0},
            self_range_median={"pbr": 1.0},
        )
        estimate = estimate_expected_return(_financial(), derived, close=1000.0)
        assert estimate is not None
        self.assertAlmostEqual(estimate.upside_blend, 0.375)
        self.assertAlmostEqual(estimate.upside_capped, 0.375)
        self.assertAlmostEqual(estimate.reversion_annual, REALIZATION_RATE_ANNUAL * 0.375)
        # carry = 配当 3% + 自社株買い 2% (clip 内)
        self.assertAlmostEqual(estimate.carry_annual, 0.03 + 0.02)
        self.assertAlmostEqual(estimate.er_annual, estimate.reversion_annual + 0.05)
        self.assertEqual(estimate.anchor_metrics, "pbr,per_forward")
        # FV anchor: sector 側 = close x mean(1.2/0.8, 15/10) = 1000 x 1.5
        assert estimate.fv_sector_median_yen is not None
        self.assertAlmostEqual(estimate.fv_sector_median_yen, 1500.0)
        self.assertEqual(estimate.origin, "estimate")
        self.assertEqual(estimate.model_version, "expected-return-v1")
        self.assertEqual(estimate.unit, "annual_ratio")
        self.assertIn("reversion=", estimate.assumptions)
        # 自己側 = close x (1.0/0.8) = 1250
        assert estimate.fv_self_range_yen is not None
        self.assertAlmostEqual(estimate.fv_self_range_yen, 1250.0)

    def test_upside_is_capped_and_can_be_negative(self) -> None:
        # deep discount: anchor 3 倍 → +200% だが cap で +50% に制限。
        derived = _derived(sector_median_value={"pbr": 2.4, "per_forward": 30.0})
        estimate = estimate_expected_return(_financial(), derived, close=1000.0)
        assert estimate is not None
        self.assertAlmostEqual(estimate.upside_capped, UPSIDE_CAP)
        # 割高側: anchor が current より低ければ負の reversion。
        derived = _derived(sector_median_value={"pbr": 0.4, "per_forward": 5.0})
        estimate = estimate_expected_return(_financial(), derived, close=1000.0)
        assert estimate is not None
        self.assertLess(estimate.upside_blend, 0.0)
        self.assertLess(estimate.reversion_annual, 0.0)

    def test_dilution_reduces_carry_and_is_clipped(self) -> None:
        derived = _derived(sector_median_value={"pbr": 1.0})
        financial = _financial(per_forward=None, dividend_yield=0.02, net_share_change_yoy=0.30)
        estimate = estimate_expected_return(financial, derived, close=1000.0)
        assert estimate is not None
        # 希薄化 30% は clip で -5% まで。carry = 0.02 - 0.05 = -0.03
        self.assertAlmostEqual(estimate.carry_annual, 0.02 - BUYBACK_CLIP)

    def test_returns_none_without_any_anchor(self) -> None:
        estimate = estimate_expected_return(_financial(), _derived(), close=1000.0)
        self.assertIsNone(estimate)

    def test_trailing_per_used_when_forward_missing(self) -> None:
        derived = _derived(sector_median_value={"pbr": 1.0, "per_trailing": 12.0})
        financial = _financial(per_forward=None, per_trailing=8.0)
        estimate = estimate_expected_return(financial, derived, close=1000.0)
        assert estimate is not None
        self.assertEqual(estimate.anchor_metrics, "pbr,per_trailing")


if __name__ == "__main__":
    unittest.main()
