"""Complete same-session comparisons preserve missingness, basis and condition sizes."""

from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.models.market_context.activity import (
    market_conditioned_returns,
    turnover_activity,
)
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar

ASOF = date(2026, 11, 13)
CALENDAR = {ASOF - timedelta(days=n): (ASOF - timedelta(days=n)).weekday() < 5 for n in range(100)}
FIXED = price_calendar(CALENDAR, as_of=ASOF)
DATES = FIXED.windows[60]
assert DATES is not None


def observe(own, nikkei, topix=None, *, calendar=FIXED):
    return market_conditioned_returns(
        prices=own,
        benchmark_prices={"1321": nikkei, **({} if topix is None else {"1306": topix})},
        calendar=calendar,
    )


def test_period_outperformance_is_not_down_market_resilience_and_proxies_differ():
    own = dict.fromkeys(DATES, 100.0) | dict(zip(DATES[-3:], [98.0, 110.0, 111.0], strict=True))
    nikkei = dict.fromkeys(DATES, 100.0) | dict(zip(DATES[-3:], [99.0, 101.0, 101.0], strict=True))
    topix = dict.fromkeys(DATES, 100.0) | dict(zip(DATES[-3:], [101.0, 99.0, 99.0], strict=True))
    result = observe(own, nikkei, topix)
    assert result["window_start_date"] == DATES[0].isoformat()
    assert result["window_end_date"] == ASOF.isoformat()
    assert result["calendar_differences"] == 60
    assert result["price_basis"] == "price_adjusted_close_not_total_return"
    benchmark = result["benchmarks"]["1321"]
    assert benchmark["benchmark_kind"] == "nikkei225_etf_proxy"
    assert benchmark["status"] == "available"
    assert benchmark["own_valid_close_count"] == benchmark["benchmark_valid_close_count"] == 61
    assert benchmark["down"] == {
        "days": 1,
        "own_up_days": 0,
        "outperform_days": 0,
        "own_mean_return": pytest.approx(-0.02),
        "benchmark_mean_return": pytest.approx(-0.01),
        "mean_excess_return": pytest.approx(-0.01),
    }
    assert benchmark["up"]["days"] == 1
    assert benchmark["up"]["outperform_days"] == 1
    assert benchmark["flat_market_days"] == 58
    assert result["benchmarks"]["1306"]["down"]["own_mean_return"] > 0
    assert own[ASOF] / own[DATES[0]] > nikkei[ASOF] / nikkei[DATES[0]]


@pytest.mark.parametrize("value", [None, 0.0, -1.0, float("inf"), float("nan")])
@pytest.mark.parametrize("side", ["own", "benchmark"])
def test_internal_gap_is_not_skipped_or_replaced_by_an_older_day(value, side):
    own = dict.fromkeys(DATES, 100.0)
    benchmark = dict(own)
    source = own if side == "own" else benchmark
    if value is None:
        del source[DATES[30]]
    else:
        source[DATES[30]] = value
    source[DATES[0] - timedelta(days=1)] = 100.0
    result = observe(own, benchmark, dict.fromkeys(DATES, 100.0))["benchmarks"]
    missing = result["1321"]
    assert missing["status"] == "unavailable"
    assert missing["unavailable_reason"] == "incomplete_price_window"
    assert missing["down"] is missing["up"] is None
    assert missing[f"{'own' if side == 'own' else 'benchmark'}_valid_close_count"] == 60
    assert result["1306"]["status"] == ("unavailable" if side == "own" else "available")


def test_flat_market_has_zero_condition_days_but_no_fabricated_mean():
    prices = dict.fromkeys(DATES, 100.0)
    result = observe(prices, prices)["benchmarks"]
    for side in ("down", "up"):
        assert result["1321"][side]["days"] == 0
        assert result["1321"][side]["own_mean_return"] is None
        assert result["1321"][side]["mean_excess_return"] is None
    assert result["1321"]["flat_market_days"] == 60
    assert result["1306"]["status"] == "unavailable"
    assert result["1306"]["benchmark_valid_close_count"] == 0


def test_future_values_and_unknown_calendar_cannot_supply_missing_sessions():
    prices = dict.fromkeys(DATES, 100.0)
    before = observe(prices, prices)
    future = {**prices, ASOF + timedelta(days=3): 1e6}
    assert observe(future, future) == before
    unknown = {day: business for day, business in CALENDAR.items() if day != DATES[30]}
    result = observe(prices, prices, calendar=price_calendar(unknown, as_of=ASOF))
    assert result["benchmarks"]["1321"]["unavailable_reason"] == "insufficient_calendar"
    assert result["benchmarks"]["1321"]["own_valid_close_count"] is None
    assert result["benchmarks"]["1321"]["benchmark_valid_close_count"] is None
    assert result["window_start_date"] is None


def test_nonfinite_daily_return_is_not_serialized():
    prices = dict.fromkeys(DATES, 100.0) | {DATES[-2]: 1e-308, ASOF: 1e308}
    result = observe(prices, dict.fromkeys(DATES, 100.0))
    assert result["benchmarks"]["1321"]["unavailable_reason"] == "nonfinite_return"


def test_turnover_excludes_current_and_retains_yen_basis_and_exact_dates():
    values = dict.fromkeys(DATES, 1e8) | {ASOF: 10e8, ASOF + timedelta(days=3): 100e8}
    result = turnover_activity(turnover=values, calendar=FIXED)
    assert result["basis"] == "traded_value_yen_not_share_volume"
    assert result["prior_start_date"] == DATES[-21].isoformat()
    assert result["prior_end_date"] == DATES[-2].isoformat()
    assert result["current_date"] == ASOF.isoformat()
    assert result["current_yen"] == 10e8
    assert result["prior_valid_days"] == 20
    assert result["prior_mean_yen"] == result["prior_median_yen"] == 1e8
    assert result["prior_min_yen"] == 1e8
    assert result["current_to_prior_mean_ratio"] == 10


@pytest.mark.parametrize("value", [None, -1.0, float("nan"), float("inf")])
@pytest.mark.parametrize("day", [DATES[-10], ASOF])
def test_missing_turnover_does_not_use_partial_mean_or_zero_fill(value, day):
    values = dict.fromkeys(DATES, 100.0) | {day: value}
    result = turnover_activity(turnover=values, calendar=FIXED)
    assert result["current_to_prior_mean_ratio"] is None
    if day == ASOF:
        assert result["unavailable_reason"] == "missing_current_turnover"
        assert result["prior_mean_yen"] == 100.0
    else:
        assert result["unavailable_reason"] == "incomplete_turnover_window"
        assert result["prior_valid_days"] == 19
        assert result["prior_mean_yen"] is result["prior_median_yen"] is None
        assert result["prior_min_yen"] is None


def test_turnover_zero_and_unknown_calendar_are_not_a_missing_ratio_of_zero():
    zero_baseline = turnover_activity(turnover=dict.fromkeys(DATES, 0.0), calendar=FIXED)
    assert zero_baseline["unavailable_reason"] == "zero_prior_mean"
    assert zero_baseline["current_to_prior_mean_ratio"] is None
    current_zero = turnover_activity(
        turnover=dict.fromkeys(DATES, 100.0) | {ASOF: 0.0}, calendar=FIXED
    )
    assert current_zero["status"] == "available"
    assert current_zero["current_to_prior_mean_ratio"] == 0.0
    missing = turnover_activity(
        turnover=dict.fromkeys(DATES, 100.0), calendar=price_calendar({}, as_of=ASOF)
    )
    assert missing["unavailable_reason"] == "insufficient_calendar"
    assert missing["current_date"] is None
    assert missing["prior_valid_days"] is None
