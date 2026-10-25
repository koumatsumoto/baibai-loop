from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.technical_analysis.momentum import price_momentum_12_1
from baibai_engine.market.price_models import PriceBar


def calendar():
    # Calendar positions, not elapsed days, define the observation period.
    start = date(2025, 1, 1)
    return [
        day
        for offset in range(400)
        if (day := start + timedelta(days=offset)).weekday() < 5
    ][:253]


def test_momentum_uses_shared_calendar_endpoints_not_company_observation_indices():
    dates = calendar()
    bars = [PriceBar(dates[0], 100.0, None), PriceBar(dates[-22], 150.0, None)]
    result = price_momentum_12_1(bars, trading_dates=dates, as_of=dates[-1])
    assert result.status == "available"
    assert result.start_date == dates[0]
    assert result.end_date == dates[-22]
    assert result.reference_date == dates[-1]
    assert result.value == 0.5


def test_missing_exact_endpoint_does_not_fall_back_to_nearby_close():
    dates = calendar()
    bars = [PriceBar(dates[1], 100.0, None), PriceBar(dates[-22], 150.0, None)]
    result = price_momentum_12_1(bars, trading_dates=dates, as_of=dates[-1])
    assert result.status == "missing_endpoint"
    assert result.start_date == dates[0]
    assert result.start_price is None
    assert result.value is None


def test_short_calendar_is_not_replaced_with_calendar_day_approximation():
    dates = calendar()
    result = price_momentum_12_1([], trading_dates=dates[1:], as_of=dates[-1])
    assert result.status == "insufficient_calendar"
    assert result.value is None


def test_future_calendar_and_price_rows_do_not_change_fixed_asof_observation():
    dates = calendar()
    bars = [PriceBar(dates[0], 100.0, None), PriceBar(dates[-22], 100.0, None)]
    before = price_momentum_12_1(bars, trading_dates=dates, as_of=dates[-1])
    future = dates[-1] + timedelta(days=7)
    after = price_momentum_12_1(
        [*bars, PriceBar(future, 999.0, None)],
        trading_dates=[*dates, future],
        as_of=dates[-1],
    )
    assert after == before
    assert after.value == 0.0


@pytest.mark.parametrize("invalid", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_endpoint_is_not_a_numeric_signal(invalid):
    dates = calendar()
    bars = [PriceBar(dates[0], invalid, None), PriceBar(dates[-22], 100.0, None)]
    result = price_momentum_12_1(bars, trading_dates=dates, as_of=dates[-1])
    assert result.status == "invalid_price"
    assert result.value is None


def test_duplicate_endpoint_is_not_silently_last_writer_wins():
    dates = calendar()
    bars = [PriceBar(dates[0], 100.0, None), PriceBar(dates[0], 200.0, None)]
    with pytest.raises(ValueError, match="one price per endpoint"):
        price_momentum_12_1(bars, trading_dates=dates, as_of=dates[-1])
