"""Retain the unrecovered path alongside recovery, without forecasting its end."""

from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.models.technical_analysis.path_statistics import (
    close_path_statistics,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar

DAYS = tuple(
    day
    for offset in range(20)
    if (day := date(2026, 10, 30) + timedelta(days=offset)).weekday() < 5
)


def episodes(prices, *, window=None):
    bars = {day: PriceBar(day, price, None) for day, price in zip(DAYS, prices, strict=False)}
    return close_path_statistics(bars, window=window or DAYS[: len(prices)])["drawdown_episodes"]


def test_current_and_latest_recovered_are_distinct_and_use_trading_not_civil_days():
    result = episodes([100, 120, 108, 110, 120, 130, 104, 117])
    assert result["latest_recovered"] == {
        "peak_date": DAYS[1].isoformat(),
        "peak_price": 120,
        "peak_at_window_start": False,
        "trough_date": DAYS[2].isoformat(),
        "trough_price": 108,
        "depth": pytest.approx(-0.1),
        "peak_to_trough_trading_days": 1,
        "recovery_date": DAYS[4].isoformat(),
        "trough_to_recovery_trading_days": 2,
        "observed_through_date": DAYS[4].isoformat(),
        "peak_to_observation_trading_days": 3,
    }
    assert result["current"] == {
        "peak_date": DAYS[5].isoformat(),
        "peak_price": 130,
        "peak_at_window_start": False,
        "trough_date": DAYS[6].isoformat(),
        "trough_price": 104,
        "depth": pytest.approx(-0.2),
        "peak_to_trough_trading_days": 1,
        "recovery_date": None,
        "trough_to_recovery_trading_days": None,
        "observed_through_date": DAYS[7].isoformat(),
        "peak_to_observation_trading_days": 2,
    }
    assert (DAYS[6] - DAYS[5]).days == 3


def test_window_edge_equal_peak_and_first_equal_low_have_explicit_meaning():
    prices = [100, 90, 80, 80, 100, 100, 95, 100]
    first = episodes(prices, window=DAYS[:5])
    recovered = first["latest_recovered"]
    assert first["current"] is None
    assert recovered["peak_at_window_start"]
    assert recovered["trough_date"] == DAYS[2].isoformat()
    assert recovered["recovery_date"] == DAYS[4].isoformat()
    assert recovered["peak_to_observation_trading_days"] == 4
    second = episodes(prices)
    assert second["current"] is None
    recovered = second["latest_recovered"]
    assert recovered["peak_date"] == DAYS[5].isoformat()
    assert not recovered["peak_at_window_start"]
    assert recovered["trough_to_recovery_trading_days"] == 1


def test_unrecovered_trough_is_only_known_so_far_and_future_values_do_not_rewrite_it():
    prices = [100, 120, 108, 110, 120, 130, 104, 117, 90, 140]
    before = episodes(prices[:8])
    assert episodes(prices, window=DAYS[:8]) == before
    later = episodes(prices[:9])
    assert later["current"]["trough_date"] == DAYS[8].isoformat()
    assert later["current"]["recovery_date"] is None
    assert later["latest_recovered"] == before["latest_recovered"]
    recovered = episodes(prices)
    assert recovered["current"] is None
    assert recovered["latest_recovered"]["recovery_date"] == DAYS[9].isoformat()
    assert recovered["latest_recovered"]["peak_price"] == 130


@pytest.mark.parametrize("prices", [[100] * 8, list(range(100, 108))])
def test_no_drawdown_is_distinct_from_unavailable_path(prices):
    assert episodes(prices) == {"current": None, "latest_recovered": None}


def test_declining_window_does_not_invent_a_recovery_or_use_an_outside_peak():
    result = episodes([200, 100, 90, 80], window=DAYS[1:4])
    assert result["latest_recovered"] is None
    current = result["current"]
    assert current["peak_price"] == 100
    assert current["peak_at_window_start"]
    assert current["depth"] == pytest.approx(-0.2)
    assert current["recovery_date"] is None
    assert current["trough_to_recovery_trading_days"] is None
    assert current["peak_to_observation_trading_days"] == 2
