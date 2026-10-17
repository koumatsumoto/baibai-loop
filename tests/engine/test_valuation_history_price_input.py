"""Valuation history uses the same observed price as its financial ratios."""

from datetime import date

from baibai_engine.market.bars import JQuantsDailyBar
from baibai_engine.valuation.metrics.history import build_valuation_history, self_range_position
from baibai_engine.valuation.metrics.models import FinancialSnapshot


def _snapshot(price):
    return FinancialSnapshot(
        latest_financial_disclosure_date=None,
        per_forward=10.0,
        per_trailing=None,
        pbr=2.0,
        ev_ebitda=None,
        p_s=None,
        pcfr=None,
        eps=None,
        sales_ttm=None,
        ocf_ttm=None,
        market_price_yen=price,
    )


def test_history_uses_snapshot_price_and_does_not_reconstruct_a_missing_price():
    day = date(2026, 10, 15)
    bars = [
        JQuantsDailyBar(
            ticker="7203", traded_at=day, close=1000.0, turnover_value=None, adjustment_factor=1.0
        )
    ]
    history = build_valuation_history(bars, _snapshot(1000.0), day, adjustment_events=bars)
    assert history["pbr"] == [2.0]
    assert history["per_forward"] == [10.0]
    missing = build_valuation_history(bars, _snapshot(None), day, adjustment_events=bars)
    assert missing["pbr"] == []
    assert missing["per_forward"] == []


def test_range_position_uses_extrema_not_observation_rank_and_is_not_clipped():
    history = [0.0, 1.0, 2.0, 100.0]
    assert self_range_position(history, 50.0) == 0.5
    assert self_range_position(history, 120.0) == 1.2
    assert self_range_position(history, None) is None
