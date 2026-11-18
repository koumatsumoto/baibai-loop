"""日次の年率換算に欠けた営業日・旧日・未知calendarを圧縮しない。"""

import math
import statistics
from datetime import date, timedelta
from itertools import pairwise

import pytest

from baibai_engine.machine_analysis.models.market_context.security import (
    calculate_security_market_context,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar

ASOF = date(2026, 11, 16)
# A known midweek closure is not a missing trading day.
CALENDAR = {
    day: day.weekday() < 5 and day != date(2026, 11, 3)
    for day in (ASOF - timedelta(days=n) for n in range(60))
}
WINDOW = price_calendar(CALENDAR, as_of=ASOF).windows[20]
BARS = tuple(PriceBar(day, 100.0 + index % 3, None) for index, day in enumerate(WINDOW))


def context(bars=BARS, calendar=CALENDAR, as_of=ASOF):
    return calculate_security_market_context(
        ticker="1301",
        bars=bars,
        calendar=price_calendar(calendar, as_of=as_of),
        master=None,
        benchmark_ticker="1321",
        benchmark_prices={},
        peer_returns=(),
    )


def test_complete_common_sessions_preserve_simple_return_sample_volatility():
    price = context()["price"]
    expected = statistics.stdev(
        right.price / left.price - 1 for left, right in pairwise(BARS)
    ) * math.sqrt(252)
    assert price["realized_vol_20d"] == expected
    assert price["realized_vol_20d_context"] == {
        "window_basis": "common_trading_calendar",
        "calendar_differences": 20,
        "window_start_date": WINDOW[0].isoformat(),
        "window_end_date": ASOF.isoformat(),
        "valid_close_count": 21,
        "return_basis": "simple_adjusted_close_return",
        "standard_deviation_ddof": 1,
        "annualization_sessions": 252,
        "unavailable_reason": None,
    }
    # Weekends/confirmed holidays have no bars and are not zero-filled returns.
    assert (WINDOW[-1] - WINDOW[0]).days > 20


@pytest.mark.parametrize("position", [0, 10, 20])
def test_older_bar_cannot_replace_missing_calendar_close(position):
    older = PriceBar(WINDOW[0] - timedelta(days=1), 50.0, None)
    bars = (older, *(bar for i, bar in enumerate(BARS) if i != position))
    price = context(bars)["price"]
    assert len(bars) == 21  # The old observed-bar formula would return a number.
    assert price["realized_vol_20d"] is None
    assert price["realized_vol_20d_context"]["unavailable_reason"] == "incomplete_close_window"
    assert price["realized_vol_20d_context"]["valid_close_count"] == 20
    # Independent observed-bar endpoint/range and latest-date conventions do not move.
    assert price["return_20d"] == bars[-1].price / older.price - 1
    assert price["resolved_date"] == bars[-1].traded_at.isoformat()


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_interior_close_is_not_skipped_or_zero_filled(bad):
    bars = tuple(
        PriceBar(bar.traded_at, bad if i == 10 else bar.price, bar.turnover_value)
        for i, bar in enumerate(BARS)
    )
    price = context(bars)["price"]
    assert price["realized_vol_20d"] is None
    assert price["realized_vol_20d_context"]["valid_close_count"] == 20
    assert price["return_20d"] == BARS[-1].price / BARS[0].price - 1


@pytest.mark.parametrize("missing", ["all", "holiday", "current", "short"])
def test_unknown_calendar_never_falls_back_to_observed_bars(missing):
    calendar = dict(CALENDAR)
    if missing == "all":
        calendar = {}
    elif missing == "short":
        calendar = {day: business for day, business in calendar.items() if day > WINDOW[0]}
    else:
        calendar.pop(ASOF if missing == "current" else date(2026, 11, 3))
    price = context(calendar=calendar)["price"]
    assert price["realized_vol_20d"] is None
    detail = price["realized_vol_20d_context"]
    assert detail["unavailable_reason"] == "insufficient_calendar"
    assert detail["window_start_date"] is detail["window_end_date"] is None
    assert detail["valid_close_count"] is None
    assert price["return_20d"] is not None


def test_known_closed_asof_uses_last_session_and_outside_bars_do_not_change_volatility():
    friday = date(2026, 11, 13)
    calendar = price_calendar(CALENDAR, as_of=friday)
    bars = tuple(PriceBar(day, 100.0 + i % 3, None) for i, day in enumerate(calendar.windows[20]))
    future = PriceBar(ASOF, 1e6, None)
    weekend = context((*bars, future), as_of=date(2026, 11, 15))["price"]
    exact = context(bars, as_of=friday)["price"]
    assert weekend["realized_vol_20d"] == exact["realized_vol_20d"]
    assert weekend["realized_vol_20d_context"] == exact["realized_vol_20d_context"]


def test_flat_is_zero_and_missing_history_is_not_zero():
    flat = tuple(PriceBar(day, 100.0, None) for day in WINDOW)
    assert context(flat)["price"]["realized_vol_20d"] == 0.0
    assert context(flat[1:])["price"]["realized_vol_20d"] is None
    assert context(())["price"] is None


def test_nonfinite_simple_returns_are_unavailable():
    bars = tuple(PriceBar(day, 1e-300 if i == 10 else 1e300, None) for i, day in enumerate(WINDOW))
    price = context(bars)["price"]
    assert price["realized_vol_20d"] is None
    assert price["realized_vol_20d_context"]["unavailable_reason"] == "nonfinite_returns"
