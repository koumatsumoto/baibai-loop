"""Same-day stock direction splits market-down yen, without a selection threshold."""

from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.models.market_context.activity import market_conditioned_returns
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar

ASOF = date(2026, 11, 13)
CALENDAR = {ASOF - timedelta(days=n): (ASOF - timedelta(days=n)).weekday() < 5 for n in range(100)}
FIXED = price_calendar(CALENDAR, as_of=ASOF)
DATES = FIXED.windows[60]
assert DATES is not None


def observe(*, own=None, turnover=None, benchmarks=None, calendar=FIXED):
    return market_conditioned_returns(
        prices=dict.fromkeys(DATES, 100.0) if own is None else own,
        turnover={} if turnover is None else turnover,
        benchmark_prices={"1321": dict.fromkeys(DATES, 100.0)}
        if benchmarks is None
        else benchmarks,
        calendar=calendar,
    )["benchmarks"]


def test_same_day_partition_separates_positive_from_flat_and_negative_not_outperformance():
    own = dict.fromkeys(DATES, 100.0) | dict(
        zip(DATES[-5:], [110, 110, 100, 120, 130], strict=True)
    )
    nikkei = dict.fromkeys(DATES, 100.0) | dict(zip(DATES[-5:], [90, 80, 70, 80, 80], strict=True))
    topix = dict.fromkeys(DATES, 100.0) | dict(
        zip(DATES[-5:], [110, 120, 130, 120, 120], strict=True)
    )
    values = dict(zip(DATES[-5:], [300, 0, 900, 100000, 1000000], strict=True))
    result = observe(own=own, turnover=values, benchmarks={"1321": nikkei, "1306": topix})
    joint = result["1321"]["down_own_direction_turnover"]
    assert joint["basis"] == "traded_value_yen_not_share_volume"
    assert joint["window_basis"] == "return_end_dates_in_common_trading_window"
    assert joint["direction_status"] == "available"
    assert joint["own_up"] == {
        "days": 1,
        "valid_turnover_days": 1,
        "mean_yen": 300,
        "median_yen": 300,
        "status": "available",
        "unavailable_reason": None,
    }
    assert joint["own_nonpositive"]["days"] == 2
    assert joint["own_nonpositive"]["mean_yen"] == joint["own_nonpositive"]["median_yen"] == 450
    assert result["1321"]["down"]["outperform_days"] == 3
    assert result["1321"]["down"]["own_up_days"] == 1
    assert result["1321"]["turnover"]["down"]["mean_yen"] == 400
    assert result["1306"]["down_own_direction_turnover"]["own_up"]["mean_yen"] == 100000
    # First close has no return; market-up/flat and outside/future dates cannot enter either group.
    outside = {DATES[0]: 1e12, DATES[0] - timedelta(days=1): 1e12, ASOF + timedelta(days=1): 1e12}
    assert (
        observe(
            own=own | outside, turnover=values | outside, benchmarks={"1321": nikkei, "1306": topix}
        )
        == result
    )


@pytest.mark.parametrize("bad", [None, -1.0, float("nan"), float("inf")])
def test_missing_yen_keeps_direction_counts_without_partial_mean_or_erasing_other_group(bad):
    own = dict.fromkeys(DATES, 100.0) | dict(zip(DATES[-3:], [110, 120, 110], strict=True))
    benchmark = dict.fromkeys(DATES, 100.0) | dict(zip(DATES[-3:], [90, 80, 70], strict=True))
    result = observe(
        own=own,
        turnover=dict(zip(DATES[-3:], [100, bad, 0], strict=True)),
        benchmarks={"1321": benchmark},
    )["1321"]
    joint = result["down_own_direction_turnover"]
    assert result["status"] == joint["direction_status"] == "available"
    assert joint["own_up"]["days"] == 2
    assert joint["own_up"]["valid_turnover_days"] == 1
    assert joint["own_up"]["mean_yen"] is joint["own_up"]["median_yen"] is None
    assert joint["own_up"]["unavailable_reason"] == "missing_or_invalid_turnover"
    assert joint["own_nonpositive"]["status"] == "available"
    assert joint["own_nonpositive"]["mean_yen"] == joint["own_nonpositive"]["median_yen"] == 0


@pytest.mark.parametrize("side", ["own", "benchmark"])
@pytest.mark.parametrize("bad", [None, 0.0, float("nan")])
def test_incomplete_prices_do_not_infer_directions_or_fill_from_another_etf(side, bad):
    own = dict.fromkeys(DATES, 100.0)
    benchmark = dict.fromkeys(DATES, 100.0) | {DATES[-2]: 90.0}
    (own if side == "own" else benchmark)[DATES[30]] = bad
    result = observe(own=own, turnover=dict.fromkeys(DATES, 100.0), benchmarks={"1321": benchmark})
    joint = result["1321"]["down_own_direction_turnover"]
    assert joint["direction_status"] == "unavailable"
    assert joint["direction_unavailable_reason"] == "incomplete_price_window"
    assert joint["own_up"] is joint["own_nonpositive"] is None
    assert result["1306"]["down_own_direction_turnover"]["own_up"] is None
    if side == "own":
        assert result["1321"]["turnover"]["down"]["mean_yen"] == 100


def test_no_condition_days_and_unknown_calendar_are_not_observed_zero():
    joint = observe(turnover=dict.fromkeys(DATES, 0.0))["1321"]["down_own_direction_turnover"]
    for name in ("own_up", "own_nonpositive"):
        assert joint[name]["days"] == joint[name]["valid_turnover_days"] == 0
        assert joint[name]["mean_yen"] is joint[name]["median_yen"] is None
        assert joint[name]["unavailable_reason"] == "no_condition_days"
    unknown = {day: business for day, business in CALENDAR.items() if day != DATES[30]}
    joint = observe(calendar=price_calendar(unknown, as_of=ASOF))["1321"][
        "down_own_direction_turnover"
    ]
    assert joint["direction_unavailable_reason"] == "insufficient_calendar"
    assert joint["own_up"] is joint["own_nonpositive"] is None


@pytest.mark.parametrize("side", ["own", "benchmark"])
def test_nonfinite_return_is_not_a_positive_or_negative_direction(side):
    own = dict.fromkeys(DATES, 100.0)
    benchmark = dict.fromkeys(DATES, 100.0)
    (own if side == "own" else benchmark).update({DATES[-2]: 1e-308, DATES[-1]: 1e308})
    joint = observe(own=own, benchmarks={"1321": benchmark})["1321"]["down_own_direction_turnover"]
    assert joint["direction_unavailable_reason"] == "nonfinite_return"
    assert joint["own_up"] is joint["own_nonpositive"] is None


def test_mean_and_median_do_not_hide_one_large_positive_session():
    own = dict.fromkeys(DATES, 100.0) | dict(zip(DATES[-3:], [110, 120, 130], strict=True))
    benchmark = dict.fromkeys(DATES, 100.0) | dict(zip(DATES[-3:], [90, 80, 70], strict=True))
    joint = observe(
        own=own,
        turnover=dict(zip(DATES[-3:], [100, 100, 700], strict=True)),
        benchmarks={"1321": benchmark},
    )["1321"]["down_own_direction_turnover"]
    assert joint["own_up"]["days"] == joint["own_up"]["valid_turnover_days"] == 3
    assert joint["own_up"]["mean_yen"] == 300
    assert joint["own_up"]["median_yen"] == 100
