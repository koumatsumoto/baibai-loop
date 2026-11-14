"""Path shape and activity are dated observations, not momentum or accumulation scores."""

from datetime import date, timedelta
from math import log

import pytest

from baibai_engine.machine_analysis.models.technical_analysis.path_statistics import (
    close_path_statistics,
)
from baibai_engine.machine_analysis.models.technical_analysis.price_path import observe_price_path
from baibai_engine.machine_analysis.observations.prices.price_basis import asof_basis_prices
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar

ASOF = date(2026, 11, 12)
CALENDAR = {ASOF - timedelta(days=n): (ASOF - timedelta(days=n)).weekday() < 5 for n in range(120)}
WINDOW = price_calendar(CALENDAR, as_of=ASOF).windows[60]
assert WINDOW is not None


def observe(closes, turnovers=None, *, states=None, future=False):
    turnovers = [1000.0] * len(closes) if turnovers is None else turnovers
    bars = [
        PriceBar(day, close, value)
        for day, close, value in zip(WINDOW, closes, turnovers, strict=True)
    ]
    if future:
        bars.append(PriceBar(ASOF + timedelta(days=1), 999999.0, 999999.0))
    return observe_price_path(
        bars,
        calendar=price_calendar(CALENDAR, as_of=ASOF),
        day_states=dict.fromkeys(WINDOW, "priced") if states is None else states,
    )


def statistics(closes, turnovers=None):
    return observe(closes, turnovers).payload["observations"]["G60"]["path_statistics"]


def test_monotone_jump_and_roundtrip_do_not_share_the_same_shape_description():
    smooth = statistics([100 * 1.01**n for n in range(61)])
    jump = statistics([100 * 1.00001**n for n in range(60)] + [100 * 1.01**60])
    assert smooth["log_path_efficiency"] == pytest.approx(1)
    assert jump["log_path_efficiency"] == pytest.approx(1)
    assert smooth["up_days"] == jump["up_days"] == 60
    assert smooth["largest_absolute_log_change_share"] == pytest.approx(1 / 60)
    assert jump["largest_absolute_log_change_share"] > 0.99
    assert smooth["max_close_drawdown"] == 0
    down = statistics([100 * 0.99**n for n in range(61)])
    assert down["log_path_efficiency"] == pytest.approx(-1)
    assert down["max_close_drawdown"] == pytest.approx(0.99**60 - 1)
    assert down["down_days"] == 60
    roundtrip = statistics([100] * 59 + [120, 100])
    assert roundtrip["log_path_efficiency"] == 0
    assert roundtrip["max_close_drawdown"] == pytest.approx(100 / 120 - 1)
    assert (roundtrip["up_days"], roundtrip["down_days"], roundtrip["flat_days"]) == (1, 1, 58)
    flat = statistics([100] * 61)
    assert flat["status"] == "available"
    assert flat["log_path_efficiency"] is None
    assert flat["largest_absolute_log_change_share"] is None
    assert flat["efficiency_unavailable_reason"] == "constant_price"
    assert flat["max_close_drawdown"] == 0


def test_activity_uses_current_day_mean_value_not_sums_or_imputed_shares():
    result = statistics([100] * 58 + [110, 121, 100], [9999999] + [1000] * 60)
    activity = result["turnover_comparison"]
    assert (result["up_days"], result["down_days"], result["flat_days"]) == (2, 1, 57)
    assert result["log_path_efficiency"] == 0
    assert result["largest_absolute_log_change_share"] == pytest.approx(0.5)
    assert result["max_close_drawdown"] == pytest.approx(100 / 121 - 1)
    assert activity == {
        "basis": "traded_value_yen_not_share_volume",
        "status": "available",
        "up_valid_turnover_days": 2,
        "down_valid_turnover_days": 1,
        "up_mean_yen": 1000,
        "down_mean_yen": 1000,
        "up_down_mean_ratio": 1,
        "unavailable_reason": None,
    }
    # The first close is only the return anchor; its turnover is not a 61st interval.
    zero_up = statistics([100] * 58 + [110, 121, 100], [1000] * 58 + [0, 0, 1000])
    assert zero_up["turnover_comparison"]["up_down_mean_ratio"] == 0


@pytest.mark.parametrize("bad", [None, -1.0, float("nan"), float("inf")])
def test_incomplete_activity_does_not_drop_days_to_create_a_favorable_ratio(bad):
    result = statistics([100] * 58 + [110, 121, 100], [1000] * 58 + [1000, bad, 1000])
    assert result["status"] == "available"
    assert result["up_days"] == 2
    assert result["turnover_comparison"]["up_mean_yen"] is None
    assert result["turnover_comparison"]["down_mean_yen"] == 1000
    assert result["turnover_comparison"]["up_valid_turnover_days"] == 1
    assert result["turnover_comparison"]["up_down_mean_ratio"] is None
    assert result["turnover_comparison"]["unavailable_reason"] == "missing_or_invalid_turnover"


def test_one_sided_and_zero_denominator_are_undefined_not_zero_demand():
    one_sided = statistics(list(range(100, 161)))["turnover_comparison"]
    assert one_sided["up_down_mean_ratio"] is None
    assert one_sided["unavailable_reason"] == "missing_up_or_down_days"
    zero = statistics([100] * 59 + [110, 100], [1000] * 60 + [0])["turnover_comparison"]
    assert zero["down_mean_yen"] == 0
    assert zero["up_down_mean_ratio"] is None
    assert zero["unavailable_reason"] == "zero_down_mean"


@pytest.mark.parametrize("state", ["absent", "no_trade"])
def test_missing_calendar_interval_never_becomes_a_smooth_filled_path(state):
    states = dict.fromkeys(WINDOW, "priced")
    if state == "absent":
        del states[WINDOW[10]]
    else:
        states[WINDOW[10]] = state
    result = observe(list(range(100, 161)), states=states).payload["observations"]["G60"]
    assert result["window_return"] == pytest.approx(0.6)
    assert result["coverage"]["row_counts"][state] == 1
    assert result["path_statistics"]["status"] == "incomplete_price_window"
    assert result["path_statistics"]["log_path_efficiency"] is None
    assert result["path_statistics"]["turnover_comparison"] is None
    assert result["path_statistics"]["drawdown_episodes"] is None


@pytest.mark.parametrize("state", ["mixed", "invalid"])
def test_other_daily_fields_do_not_invalidate_an_observed_valid_close(state):
    states = dict.fromkeys(WINDOW, "priced")
    states[WINDOW[10]] = state
    result = observe(list(range(100, 161)), states=states).payload["observations"]["G60"]
    assert result["coverage"]["row_counts"][state] == 1
    assert not result["coverage"]["complete"]
    assert result["path_statistics"]["status"] == "available"
    assert result["path_statistics"]["price_row_basis"] == "observed_finite_positive_adjusted_close"
    assert result["path_statistics"]["up_days"] == 60


def test_future_values_and_outside_window_prices_do_not_change_statistics():
    closes = [100] * 59 + [110, 120]
    assert observe(closes).payload == observe(closes, future=True).payload
    result = observe(closes).payload["observations"]["G60"]
    assert result["window_start_date"] == WINDOW[0].isoformat()
    assert result["window_end_date"] == ASOF.isoformat()
    assert result["calendar_differences"] == 60
    assert result["coverage"]["expected_dates"] == 61
    assert result["path_statistics"]["largest_absolute_log_change_share"] == pytest.approx(
        log(1.1) / log(1.2)
    )


def test_split_price_adjustment_does_not_rescale_traded_yen_or_use_future_action():
    raw = [400] * 59 + [110, 100]
    actions = [(WINDOW[59], 0.25), (ASOF + timedelta(days=1), 0.5)]
    adjusted = asof_basis_prices(list(zip(WINDOW, raw, strict=True)), actions, asof_date=ASOF)
    result = statistics(adjusted)
    assert result == statistics([100] * 59 + [110, 100])
    assert result["turnover_comparison"]["up_down_mean_ratio"] == 1


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_price_is_not_a_flat_observation(price):
    bars = {day: PriceBar(day, 100.0, 1000.0) for day in WINDOW}
    bars[WINDOW[2]] = PriceBar(WINDOW[2], price, 1000.0)
    result = close_path_statistics(bars, window=WINDOW)
    assert result["status"] == "invalid_price"
    assert result["drawdown_episodes"] is None


def test_unknown_calendar_has_no_invented_shape():
    result = close_path_statistics({}, window=None)
    assert result["status"] == "insufficient_calendar"
    assert result["up_days"] is None
    assert result["log_path_efficiency"] is None
    assert result["drawdown_episodes"] is None
