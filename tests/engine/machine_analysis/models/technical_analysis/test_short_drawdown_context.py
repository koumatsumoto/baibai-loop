"""A recent recovery inside a long drawdown is context, not a trading signal."""

from copy import deepcopy
from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.models.technical_analysis import path_statistics
from baibai_engine.machine_analysis.models.technical_analysis.path_statistics import (
    close_path_statistics,
)
from baibai_engine.machine_analysis.models.technical_analysis.price_path import observe_price_path
from baibai_engine.machine_analysis.observations.prices.price_basis import asof_basis_prices
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar

ASOF = date(2026, 11, 13)
CALENDAR = {ASOF - timedelta(days=n): (ASOF - timedelta(days=n)).weekday() < 5 for n in range(120)}
CAL = price_calendar(CALENDAR, as_of=ASOF)
WINDOW = CAL.windows[60]
assert WINDOW is not None


def _bars(closes):
    return [PriceBar(day, close, None) for day, close in zip(WINDOW, closes, strict=True)]


def _observe(bars, *, calendar=CAL, states=None):
    return observe_price_path(
        bars,
        calendar=calendar,
        day_states=dict.fromkeys(WINDOW, "priced") if states is None else states,
    )


def _context(result):
    return result.payload["observations"]["R20"]["path_statistics"]


def test_recent_recovery_then_failure_is_not_a_recovered_long_trend():
    bars = _bars([200] * 40 + [100, 95, 90, 100, 110, 100] + [105] * 15)
    result = _observe(bars)
    r20 = result.payload["observations"]["R20"]
    context = _context(result)
    assert set(context) == {"status", "price_row_basis", "drawdown_episodes"}
    assert context["status"] == "available"
    assert r20["value"] == pytest.approx(0.05)
    assert r20["window_start_date"] == WINDOW[40].isoformat()
    assert r20["calendar_differences"] == 20
    current, recovered = (
        context["drawdown_episodes"][key] for key in ("current", "latest_recovered")
    )
    assert (current["peak_date"], current["trough_date"], current["recovery_date"]) == (
        WINDOW[44].isoformat(),
        WINDOW[45].isoformat(),
        None,
    )
    assert (recovered["peak_date"], recovered["trough_date"], recovered["recovery_date"]) == (
        WINDOW[40].isoformat(),
        WINDOW[42].isoformat(),
        WINDOW[43].isoformat(),
    )
    assert recovered["peak_at_window_start"] is True
    long = result.payload["observations"]["G60"]["path_statistics"]
    assert long == close_path_statistics({bar.traded_at: bar for bar in bars}, window=WINDOW)
    assert long["drawdown_episodes"]["current"]["peak_price"] == 200
    assert long["drawdown_episodes"]["latest_recovered"] is None
    assert set(result.payload["observations"]) == {"R1", "R20", "G60", "G252"}
    assert [pair.observation_ref for pair in result.comparison_pairs] == ["R1", "R20", "G60"]


def test_short_recovery_can_be_complete_while_long_drawdown_remains_open():
    result = _observe(_bars([200] * 40 + [100, 80] + [100] * 19))
    short = _context(result)["drawdown_episodes"]
    assert short["current"] is None
    assert short["latest_recovered"]["recovery_date"] == WINDOW[42].isoformat()
    assert (
        result.payload["observations"]["G60"]["path_statistics"]["drawdown_episodes"]["current"]
        is not None
    )


def test_constant_path_means_no_episode_not_unknown_and_equal_extrema_keep_dates():
    flat = _context(_observe(_bars([100] * 61)))
    assert flat["status"] == "available"
    assert flat["drawdown_episodes"] == {"current": None, "latest_recovered": None}
    result = _context(_observe(_bars([200] * 40 + [100, 100, 90, 90] + [95] * 17)))
    current = result["drawdown_episodes"]["current"]
    assert current["peak_date"] == WINDOW[41].isoformat()
    assert current["trough_date"] == WINDOW[42].isoformat()
    assert current["observed_through_date"] == ASOF.isoformat()
    assert current["recovery_date"] is None


@pytest.mark.parametrize("state", ["absent", "no_trade"])
def test_interior_missing_close_keeps_endpoint_return_but_not_recovery(state):
    states = dict.fromkeys(WINDOW, "priced")
    states.pop(WINDOW[50])
    if state != "absent":
        states[WINDOW[50]] = state
    result = _observe(_bars(list(range(100, 161))), states=states)
    assert result.payload["observations"]["R20"]["value"] == pytest.approx(160 / 140 - 1)
    assert _context(result)["status"] == "incomplete_price_window"
    assert _context(result)["drawdown_episodes"] is None


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_close_is_not_a_recovered_or_flat_day(price):
    closes = [100] * 61
    closes[50] = price
    context = _context(_observe(_bars(closes)))
    assert context["status"] == "incomplete_price_window"
    assert context["drawdown_episodes"] is None


@pytest.mark.parametrize("state", ["mixed", "invalid"])
def test_valid_close_is_independent_of_missing_other_daily_fields(state):
    states = dict.fromkeys(WINDOW, "priced")
    states[WINDOW[50]] = state
    result = _observe(_bars([100] * 61), states=states)
    assert not result.payload["observations"]["R20"]["coverage"]["complete"]
    assert _context(result)["status"] == "available"
    assert _context(result)["price_row_basis"] == "observed_finite_positive_adjusted_close"


def test_each_calendar_window_is_independent_and_unknown_is_not_no_episode(monkeypatch):
    def unused_turnover(*_):
        pytest.fail("短期の回復だけを読むために長期向けの売買代金比較を作らない")

    monkeypatch.setattr(path_statistics, "_turnover_comparison", unused_turnover)
    bars = _bars([100] * 61)
    short = price_calendar(
        {day: value for day, value in CALENDAR.items() if day >= WINDOW[40]}, as_of=ASOF
    )
    result = _observe(bars, calendar=short)
    assert _context(result)["status"] == "available"
    assert (
        result.payload["observations"]["G60"]["path_statistics"]["status"]
        == "insufficient_calendar"
    )
    unknown = _context(_observe(bars, calendar=price_calendar({}, as_of=ASOF)))
    assert unknown["status"] == "insufficient_calendar"
    assert unknown["drawdown_episodes"] is None


def test_future_outside_window_and_unknown_later_recovery_do_not_rewrite_current():
    bars = _bars([200] * 40 + [100] * 19 + [90, 95])
    original = _observe(bars)
    saved = deepcopy(_context(original))
    changed = [
        PriceBar(bar.traded_at, 10000 if index < 40 else bar.price, None)
        for index, bar in enumerate(bars)
    ]
    changed.append(PriceBar(ASOF + timedelta(days=3), 10000, None))
    assert _context(_observe(changed)) == saved
    assert saved["drawdown_episodes"]["current"]["recovery_date"] is None
    assert _context(original) == saved


def test_split_adjustment_and_future_action_do_not_create_short_recovery():
    raw = [400] * 59 + [90, 100]
    actions = [(WINDOW[59], 0.25), (ASOF + timedelta(days=1), 0.5)]
    adjusted = asof_basis_prices(list(zip(WINDOW, raw, strict=True)), actions, asof_date=ASOF)
    assert _context(_observe(_bars(adjusted))) == _context(_observe(_bars([100] * 59 + [90, 100])))
