"""Only share-unit changes invalidate share-denominated margin comparisons."""

from datetime import date

import pytest

from baibai_engine.market.bars import JQuantsAdjustmentFactorEvent, JQuantsDailyBar
from baibai_engine.market_context.history import has_share_basis_warning
from baibai_engine.market_context.margin import MarginBalance, margin_supply_demand


@pytest.mark.parametrize(
    ("kind", "factor", "warning"),
    [("1", 0.5, True), ("2", 2.0, True), ("3", 0.8, False), ("3", 1.0, False), (None, 0.8, True)],
)
def test_margin_comparison_uses_share_units_not_price_adjustments(kind, factor, warning):
    start, action, end = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
    bars = [
        JQuantsDailyBar("1111", start, 100.0, None, adjustment_factor=1.0),
        JQuantsDailyBar("1111", end, 100.0, None, adjustment_factor=1.0),
    ]
    events = [JQuantsAdjustmentFactorEvent("1111", action, factor, kind)]
    flagged = has_share_basis_warning(bars, end, 20, adjustment_events=events)
    assert flagged is warning
    result = margin_supply_demand(
        latest=MarginBalance(balance_date=end, issue_type="2", long_vol=1000.0, short_vol=200.0),
        prior_26w=MarginBalance(balance_date=start, long_vol=500.0),
        avg_daily_volume_shares=100.0,
        shares_outstanding=10000.0,
        adv_basis_warning=flagged,
        delta_basis_warning=flagged,
    )
    assert result.margin_long_to_adv == (None if warning else 10.0)
    assert result.margin_short_to_adv == (None if warning else 2.0)
    assert result.margin_long_delta_26w == (None if warning else 0.05)
