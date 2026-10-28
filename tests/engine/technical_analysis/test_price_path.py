"""Fixed-calendar evidence, missingness and inclusive research-note boundaries."""

from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.models.market_context.price_path import compare_price_path
from baibai_engine.machine_analysis.models.technical_analysis.price_path import observe_price_path
from baibai_engine.market.price_basis import asof_basis_prices
from baibai_engine.market.price_models import PriceBar
from baibai_engine.market.price_observation import classify_price_day, price_calendar

ASOF = date(2026, 10, 26)
CALENDAR = {ASOF - timedelta(days=n): (ASOF - timedelta(days=n)).weekday() < 5 for n in range(500)}
DATES = sorted(day for day, business in CALENDAR.items() if business)


def observe(prices, *, states=None, calendar=None):
    return observe_price_path(
        [PriceBar(day, value, None) for day, value in sorted(prices.items())],
        calendar=price_calendar(CALENDAR if calendar is None else calendar, as_of=ASOF),
        day_states=dict.fromkeys(prices, "priced") if states is None else states,
    )


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((None,) * 6, "no_trade"),
        ((100, 100, 100, 100, 10, 1000), "priced"),
        ((None, None, None, 100, None, None), "mixed"),
        ((100, 100, 100, 0, 10, 1000), "invalid"),
        ((100, 100, 100, float("nan"), 10, 1000), "invalid"),
        ((100, 100, 100, 100, -1, 1000), "invalid"),
    ],
)
def test_saved_six_field_state_is_not_a_reason_for_no_executions(values, expected):
    assert classify_price_day(values) == expected


@pytest.mark.parametrize(
    ("ref", "differences", "end"), [("R1", 1, 92), ("R20", 20, 80), ("G60", 60, 80)]
)
def test_threshold_boundary_uses_price_pairs_without_rounding_the_observation(
    ref, differences, end
):
    prices = dict.fromkeys(DATES[-253:], 99.0)
    prices[DATES[-differences - 1]] = 100.0
    prices[ASOF] = end
    at_boundary = observe(prices).payload["annotation"]
    assert ref in at_boundary["matched_refs"]
    prices[ASOF] = end + 1e-10
    just_above = observe(prices).payload["annotation"]
    assert ref not in just_above["matched_refs"]
    assert ref in just_above["evaluated_refs"]


@pytest.mark.parametrize("state", ["no_trade", "absent", "mixed", "invalid"])
def test_return_needs_endpoints_but_peak_nonmatch_needs_explained_days(state):
    prices = dict.fromkeys(DATES[-253:], 100.0)
    prices[ASOF] = 90.0
    prices[DATES[-2]] = 90.0
    hole = DATES[-10]
    del prices[hole]
    states = dict.fromkeys(prices, "priced")
    if state != "absent":
        states[hole] = state
    result = observe(prices, states=states).payload
    r20 = result["observations"]["R20"]
    assert r20["value"] == pytest.approx(-0.1)
    assert "R20" in result["annotation"]["evaluated_refs"]
    g60 = result["observations"]["G60"]
    assert g60["value"] == pytest.approx(-0.1)
    assert g60["coverage"]["row_counts"][state] == 1
    if state == "no_trade":
        assert result["annotation"]["status"] == "not_matched"
        assert g60["coverage"]["complete"]
    else:
        assert result["annotation"]["status"] == "unassessable"
        assert result["annotation"]["unavailable_refs"] == ["G60"]
    prices[ASOF] = 80.0
    assert "G60" in observe(prices, states=states).payload["annotation"]["matched_refs"]


def test_short_history_witness_and_first_observation_unknown():
    prices = {DATES[-8]: 100.0, ASOF: 75.0}
    result = observe(prices).payload
    assert result["annotation"]["matched_refs"] == ["G60"]
    assert result["observations"]["G60"]["coverage"]["row_counts"]["absent"] == 59
    first_day = observe({ASOF: 100.0}).payload
    assert first_day["annotation"]["status"] == "unassessable"
    assert first_day["observations"]["G60"]["value"] is None
    assert first_day["observations"]["G60"]["unavailable_reason"] == "no_past_price"


def test_expected_current_no_trade_never_carries_old_close_forward():
    last = DATES[-8]
    prices = {DATES[-20]: 100, last: 80}
    states = dict.fromkeys(prices, "priced") | {ASOF: "no_trade"}
    result = observe(prices, states=states).payload
    assert result["latest_observed_price_date"] == last.isoformat()
    assert result["expected_price_date"] == ASOF.isoformat()
    assert result["current_row_state"] == "no_trade"
    assert result["current_price"] is None
    assert result["freshness"] == "missing_expected_close"
    assert result["annotation"]["status"] == "unassessable"
    assert all(item["value"] is None for item in result["observations"].values())


def test_calendar_window_does_not_extend_to_old_2023_prices_and_tie_anchor_is_latest():
    prices = {date(2023, 9, 26): 2798, DATES[-200]: 2000, DATES[-20]: 2000, ASOF: 1500}
    result = observe(prices).payload
    long = result["observations"]["G252"]
    assert long["window_start_date"] == DATES[-253].isoformat()
    assert long["observed_peak_price"] == 2000
    assert long["observed_peak_date"] == DATES[-20].isoformat()
    assert long["value"] == -0.25
    assert long["window_return"] is None
    assert long["window_return_unavailable_reason"] == "missing_window_start_price"
    assert "G252" not in result["annotation"]["thresholds"]


def test_current_high_with_a_past_price_is_a_valid_zero_and_window_return_is_distinct():
    result = observe({DATES[-253]: 100, DATES[-10]: 200, ASOF: 150}).payload
    long = result["observations"]["G252"]
    assert long["value"] == -0.25
    assert long["window_return"] == 0.5
    current_high = observe({DATES[-253]: 100, ASOF: 100}).payload["observations"]["G252"]
    assert current_high["value"] == 0
    assert current_high["observed_peak_date"] == ASOF.isoformat()


def test_calendar_holes_are_scoped_to_required_windows_and_no_company_price_fills_them():
    calendar = dict(CALENDAR)
    del calendar[ASOF - timedelta(days=150)]
    prices = dict.fromkeys(DATES[-253:], 100.0)
    result = observe(prices, calendar=calendar).payload
    assert result["observations"]["R20"]["value"] == 0
    assert result["observations"]["G60"]["value"] == 0
    assert result["observations"]["G252"]["unavailable_reason"] == "insufficient_calendar"
    del calendar[ASOF]
    missing = observe(prices, calendar=calendar).payload
    assert missing["freshness"] == "unknown_calendar"
    assert missing["annotation"]["status"] == "unassessable"


@pytest.mark.parametrize("benchmark_start", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_tri_valued_or_keeps_positive_and_unavailable_refs_and_benchmark_independent(
    benchmark_start,
):
    result = observe({DATES[-2]: 100, ASOF: 75})
    annotation = result.payload["annotation"]
    assert annotation["status"] == "matched"
    assert annotation["matched_refs"] == ["R1", "G60"]
    assert annotation["unavailable_refs"] == ["R20"]
    assert set(annotation["evaluated_refs"]).isdisjoint(annotation["unavailable_refs"])
    benchmark_prices = {ASOF: 200}
    if benchmark_start is not None:
        benchmark_prices[DATES[-2]] = benchmark_start
    relative = compare_price_path(
        result.comparison_pairs,
        benchmark_prices=benchmark_prices,
        as_of=ASOF,
        benchmark_ticker="1321",
    )
    assert relative["comparisons"]["R1"]["status"] == "missing_benchmark_endpoint"
    assert annotation["status"] == "matched"


def test_split_adjusted_stock_and_benchmark_use_same_endpoints_and_keep_null_day_factor():
    before, action_day = DATES[-3], DATES[-2]
    prices = asof_basis_prices([(before, 400), (ASOF, 90)], [(action_day, 0.25)], asof_date=ASOF)
    result = observe(
        dict(zip((before, ASOF), prices, strict=True)),
        states={before: "priced", action_day: "no_trade", ASOF: "priced"},
    )
    assert result.payload["observations"]["G60"]["value"] == pytest.approx(-0.1)
    benchmark = asof_basis_prices(
        [(before, 60000), (ASOF, 660)], [(action_day, 0.01)], asof_date=ASOF
    )
    relative = compare_price_path(
        result.comparison_pairs,
        benchmark_prices=dict(zip((before, ASOF), benchmark, strict=True)),
        as_of=ASOF,
        benchmark_ticker="1321",
    )["comparisons"]["G60"]
    assert relative["benchmark_return"] == pytest.approx(0.1)
    assert relative["relative_return"] == pytest.approx(-0.2)
