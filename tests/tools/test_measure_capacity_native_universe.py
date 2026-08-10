from __future__ import annotations

from datetime import date, timedelta

import pytest
from tools.experiments.measure_capacity_native_universe import (
    CapacityStudyError,
    MarketBar,
    _capacity_fact,
    _concentration,
    _nearest_rank_percentile,
)


def _bar(day: date, *, close: float = 1_000.0, turnover: float = 10_000_000.0) -> MarketBar:
    return MarketBar(
        traded_at=day,
        close=close,
        adjusted_close=close,
        volume=turnover / close,
        turnover_value=turnover,
    )


def test_nearest_rank_percentile_is_conservative_and_rejects_empty() -> None:
    assert _nearest_rank_percentile(list(range(1, 61)), 0.20) == 12
    with pytest.raises(CapacityStudyError, match="at least one"):
        _nearest_rank_percentile([], 0.20)


def test_capacity_fact_counts_missing_session_as_no_trade() -> None:
    sessions = [date(2026, 1, 1) + timedelta(days=offset) for offset in range(60)]
    bars = {session: _bar(session) for session in sessions}
    bars.pop(sessions[10])

    fact = _capacity_fact(session_dates=sessions, bars=bars)

    assert fact.minimum_lot_yen == 100_000.0
    assert fact.trading_value_p20_60d == 10_000_000.0
    assert fact.usable_sessions == 59
    assert fact.nonzero_volume_share_60d == pytest.approx(59 / 60)
    assert fact.no_trade_share_60d == pytest.approx(1 / 60)
    assert fact.capacity_days_by_notional["standard"] == pytest.approx(3.0)


def test_capacity_fact_uses_raw_close_for_lot_and_adjusted_close_for_return() -> None:
    sessions = [date(2026, 1, 1) + timedelta(days=offset) for offset in range(60)]
    bars = {session: _bar(session) for session in sessions}
    last = sessions[-1]
    bars[last] = MarketBar(
        traded_at=last,
        close=2_000.0,
        adjusted_close=1_000.0,
        volume=10_000.0,
        turnover_value=10_000_000.0,
    )

    fact = _capacity_fact(session_dates=sessions, bars=bars)

    assert fact.minimum_lot_yen == 200_000.0
    assert fact.zero_return_share_60d == 1.0


def test_concentration_uses_repeated_monthly_selections() -> None:
    result = _concentration(["A", "A", "B", "C"])
    assert result == {"selection_count": 4, "unique_tickers": 3, "max_ticker_share": 0.5}
