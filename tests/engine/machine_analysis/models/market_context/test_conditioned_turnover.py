"""Market direction selects same-session yen values, independently of own closes."""

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


def test_turnover_uses_return_end_date_not_own_direction_or_adjacent_session():
    own = dict.fromkeys(DATES, 100.0) | {DATES[-2]: 110.0}
    nikkei = dict.fromkeys(DATES, 100.0) | {DATES[-2]: 90.0}
    topix = dict.fromkeys(DATES, 100.0) | {DATES[-2]: 110.0}
    values = {DATES[0]: 1e12, DATES[-2]: 300.0, DATES[-1]: 900.0, ASOF + timedelta(days=1): 1e12}
    result = observe(own=own, turnover=values, benchmarks={"1321": nikkei, "1306": topix})
    activity = result["1321"]["turnover"]
    assert activity["basis"] == "traded_value_yen_not_share_volume"
    assert activity["window_basis"] == "return_end_dates_in_common_trading_window"
    assert activity["market_direction_status"] == "available"
    assert activity["down"] == {
        "days": 1,
        "valid_turnover_days": 1,
        "mean_yen": 300.0,
        "median_yen": 300.0,
        "status": "available",
        "unavailable_reason": None,
    }
    assert activity["up"]["mean_yen"] == 900.0
    assert activity["flat_market_days"] == 58
    assert result["1321"]["down"]["own_up_days"] == 1
    assert result["1306"]["turnover"]["down"]["mean_yen"] == 900.0
    assert result["1306"]["turnover"]["up"]["mean_yen"] == 300.0
    # Out-of-window values and missing flat-session values do not enter a condition mean.
    assert (
        observe(
            own=own,
            turnover={DATES[-2]: 300.0, DATES[-1]: 900.0},
            benchmarks={"1321": nikkei, "1306": topix},
        )
        == result
    )


@pytest.mark.parametrize("bad", [None, -1.0, float("nan"), float("inf")])
def test_missing_turnover_preserves_price_comparison_and_complete_other_direction(bad):
    benchmark = dict.fromkeys(DATES, 100.0) | {DATES[-3]: 90.0, DATES[-2]: 80.0}
    values = {DATES[-3]: 100.0, DATES[-2]: bad, DATES[-1]: 500.0}
    result = observe(turnover=values, benchmarks={"1321": benchmark})["1321"]
    assert result["status"] == "available"
    assert result["down"]["days"] == 2
    activity = result["turnover"]
    assert activity["market_direction_status"] == "available"
    assert activity["down"]["days"] == 2
    assert activity["down"]["valid_turnover_days"] == 1
    assert activity["down"]["mean_yen"] is activity["down"]["median_yen"] is None
    assert activity["down"]["unavailable_reason"] == "missing_or_invalid_turnover"
    assert activity["up"]["mean_yen"] == 500.0


def test_price_missingness_does_not_erase_observed_turnover_or_use_another_etf():
    own = dict.fromkeys(DATES, 100.0)
    del own[DATES[30]]
    benchmark = dict.fromkeys(DATES, 100.0) | {DATES[-2]: 90.0}
    result = observe(
        own=own,
        turnover={DATES[-2]: 0.0, DATES[-1]: 100.0},
        benchmarks={"1321": benchmark},
    )
    assert result["1321"]["unavailable_reason"] == "incomplete_price_window"
    assert result["1321"]["down"] is None
    activity = result["1321"]["turnover"]
    assert activity["down"]["status"] == "available"
    assert activity["down"]["mean_yen"] == activity["down"]["median_yen"] == 0.0
    assert result["1306"]["turnover"]["market_direction_unavailable_reason"] == (
        "incomplete_benchmark_price_window"
    )
    assert result["1306"]["turnover"]["down"] is None


@pytest.mark.parametrize("bad", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_incomplete_benchmark_does_not_compress_or_guess_market_condition_days(bad):
    benchmark = dict.fromkeys(DATES, 100.0) | {DATES[30]: bad, DATES[-2]: 90.0}
    benchmark[DATES[0] - timedelta(days=1)] = 100.0
    activity = observe(turnover=dict.fromkeys(DATES, 100.0), benchmarks={"1321": benchmark})[
        "1321"
    ]["turnover"]
    assert activity["market_direction_unavailable_reason"] == "incomplete_benchmark_price_window"
    assert activity["up"] is activity["down"] is activity["flat_market_days"] is None


def test_flat_market_has_empty_groups_not_zero_turnover_and_unknown_calendar_stays_unknown():
    activity = observe(turnover=dict.fromkeys(DATES, 100.0))["1321"]["turnover"]
    assert activity["flat_market_days"] == 60
    for name in ("up", "down"):
        assert activity[name]["days"] == activity[name]["valid_turnover_days"] == 0
        assert activity[name]["mean_yen"] is activity[name]["median_yen"] is None
        assert activity[name]["unavailable_reason"] == "no_condition_days"
    unknown = {day: business for day, business in CALENDAR.items() if day != DATES[30]}
    activity = observe(calendar=price_calendar(unknown, as_of=ASOF))["1321"]["turnover"]
    assert activity["market_direction_unavailable_reason"] == "insufficient_calendar"
    assert activity["up"] is activity["down"] is None


def test_nonfinite_benchmark_return_is_not_classified_and_finite_summaries_do_not_overflow():
    benchmark = dict.fromkeys(DATES, 100.0) | {DATES[-2]: 1e-308, DATES[-1]: 1e308}
    activity = observe(benchmarks={"1321": benchmark})["1321"]["turnover"]
    assert activity["market_direction_unavailable_reason"] == "nonfinite_benchmark_return"
    benchmark = dict.fromkeys(DATES, 100.0) | {DATES[-3]: 90.0, DATES[-2]: 80.0}
    activity = observe(turnover=dict.fromkeys(DATES, 1e308), benchmarks={"1321": benchmark})[
        "1321"
    ]["turnover"]
    assert activity["down"]["mean_yen"] == activity["down"]["median_yen"] == 1e308


def test_condition_mean_and_median_keep_the_effect_of_one_large_session_distinct():
    benchmark = dict.fromkeys(DATES, 100.0) | dict(zip(DATES[-3:], [90.0, 80.0, 70.0], strict=True))
    values = dict(zip(DATES[-3:], [100.0, 100.0, 700.0], strict=True))
    down = observe(turnover=values, benchmarks={"1321": benchmark})["1321"]["turnover"]["down"]
    assert down["days"] == down["valid_turnover_days"] == 3
    assert down["mean_yen"] == 300.0
    assert down["median_yen"] == 100.0
