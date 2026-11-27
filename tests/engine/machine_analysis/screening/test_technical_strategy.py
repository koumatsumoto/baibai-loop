"""保存モデルの6つの独立な探索条件と、不要な条件の吸収を確認する。"""

from copy import deepcopy
from itertools import product
from math import inf, nextafter

import pytest
from tests.engine.machine_analysis.models.market_context.test_activity import DATES, observe
from tests.helpers.technical_discovery import _DAY, _metadata, _models
from tests.helpers.technical_financial import _no_match

from baibai_engine.machine_analysis.screening.policies.technical_discovery import (
    evaluate_technical_strategy,
)


def _strategy_models():
    models = _models()
    ta, market = (model.payload for model in models)
    ta["moving_averages"]["sma_75bar"] = {
        "status": "available",
        "gap_ratio": 0.04,
        "value": 105.0,
        "window_bars": 75,
        "window_start_date": "2026-07-28",
        "window_end_date": "2026-11-13",
    }
    ta["price_path"]["observations"]["G60"]["path_statistics"] = {
        "status": "available",
        "log_path_efficiency": 0.35,
        "largest_absolute_log_change_share": 0.1,
        "max_close_drawdown": -0.08,
        "drawdown_episodes": {"current": None, "latest_recovered": None},
    }
    market["relative"] = {
        "market_conditioned_60d": {
            "window_basis": "common_trading_calendar",
            "calendar_differences": 60,
            "window_start_date": "2026-08-14",
            "window_end_date": "2026-11-13",
            "price_basis": "price_adjusted_close_not_total_return",
            "benchmarks": {
                benchmark: {
                    "status": "available",
                    "unavailable_reason": None,
                    "down": {"days": 25, "own_mean_return": own, "mean_excess_return": excess},
                    "turnover": {
                        "basis": "traded_value_yen_not_share_volume",
                        "market_direction_status": "available",
                        "market_direction_unavailable_reason": None,
                        "down": {
                            "days": 25,
                            "valid_turnover_days": 25,
                            "mean_yen": 1_000_000.0,
                            "median_yen": 900_000.0,
                            "status": "available",
                            "unavailable_reason": None,
                        },
                    },
                }
                for benchmark, own, excess in (("1321", 0.002, 0.012), ("1306", -0.02, -0.01))
            },
        }
    }
    return models


def _down_window(models):
    return models[1].payload["relative"]["market_conditioned_60d"]


def _example(route=None):
    """Start with six observed nonmatches; change only the source relevant to a contrast."""
    models = _strategy_models()
    _no_match(models)
    ta, market = (model.payload for model in models)
    down = _down_window(models)["benchmarks"]["1321"]["down"]
    down["own_mean_return"] = 0.0
    observations = ta["price_path"]["observations"]
    if route == "outperformance":
        market["price_path_relative"]["comparisons"]["R20"]["relative_return"] = 0.01
    elif route == "continuation":
        ta["price_momentum"]["value"] = observations["R20"]["value"] = 0.01
    elif route == "pullback":
        ta["price_momentum"]["value"] = 0.01
        ta["moving_averages"]["sma_20bar"]["gap_ratio"] = -0.01
    elif route == "rebound":
        observations["R1"]["value"] = 0.01
    elif route == "turnover":
        market["price"]["turnover_activity_20d"]["current_to_prior_mean_ratio"] = 1.01
    elif route == "resilience":
        down["own_mean_return"] = 0.01
    elif route is not None:
        raise ValueError(route)
    return models


def _evaluate(models):
    return evaluate_technical_strategy("1234", models=models, metadata=_metadata())


@pytest.mark.parametrize(
    "route", ["outperformance", "continuation", "pullback", "rebound", "turnover", "resilience"]
)
def test_each_source_can_alone_change_a_known_nonmatch_to_a_match(route):
    assert _evaluate(_example()).state == "not_matched"
    models = _example(route)
    original = deepcopy(models)
    result = _evaluate(models)
    assert result.state == "matched"
    assert result.price_ready
    assert result.expected_price_date == _DAY
    assert models == original


def test_absorbed_activity_confirmation_cannot_change_any_of_162_states():
    # Turnover alone is sufficient. Vary the three former confirmation inputs independently
    # without letting R1 create a rebound or SMA200 create a momentum match.
    metadata = _metadata()
    models = _example()
    ta, market = (m.payload for m in models)
    path, moving = ta["price_path"], ta["moving_averages"]
    path["observations"]["R20"]["value"] = 0
    activity = market["price"]["turnover_activity_20d"]
    for priced, (ratio, r1, sma75, sma200) in product(
        (False, True), product((None, 0, 2), repeat=4)
    ):
        path["freshness"] = "current" if priced else "stale"
        activity["current_to_prior_mean_ratio"] = ratio
        path["observations"]["R1"]["value"] = r1
        moving["sma_75bar"]["gap_ratio"] = sma75
        moving["sma_200bar"]["gap_ratio"] = sma200
        result = evaluate_technical_strategy("1234", models=models, metadata=metadata)
        assert result.state == (
            {None: "unavailable", 0: "not_matched", 2: "matched"}[ratio]
            if priced
            else "unavailable"
        )
        assert result.price_ready == priced


@pytest.mark.parametrize(
    ("section", "field", "value", "state"),
    [
        ("down", "own_mean_return", 0, "not_matched"),
        ("down", "own_mean_return", -0.02, "not_matched"),
        ("down", "mean_excess_return", 0, "not_matched"),
        ("down", "mean_excess_return", None, "unavailable"),
        ("down", "days", 0, "unavailable"),
        ("down", "days", 61, "unavailable"),
        ("down", "days", 1.5, "unavailable"),
        ("source", "status", "unavailable", "unavailable"),
        ("window", "window_end_date", "2026-11-14", "unavailable"),
        ("window", "calendar_differences", 59, "unavailable"),
        ("window", "price_basis", "total_return", "unavailable"),
        ("window", "window_basis", "observed_sessions", "unavailable"),
    ],
)
def test_resilience_requires_absolute_and_relative_gain_on_the_stated_down_market_window(
    section, field, value, state
):
    models = _example("resilience")
    window = _down_window(models)
    source = window["benchmarks"]["1321"]
    {"down": source["down"], "source": source, "window": window}[section][field] = value
    assert _evaluate(models).state == state


@pytest.mark.parametrize(
    ("benchmark", "state"), [("1306", "not_matched"), ("unknown", "unavailable")]
)
def test_evaluates_only_the_requested_benchmark_not_the_best_index(benchmark, state):
    models = _example("resilience")
    models[1].payload["price_path_relative"]["benchmark_ticker"] = benchmark
    assert _evaluate(models).state == state


def test_down_day_turnover_and_frequency_do_not_override_observed_returns():
    models = _example("resilience")
    source = _down_window(models)["benchmarks"]["1321"]
    source.pop("turnover")
    source["down"].update(own_up_days=1, outperform_days=25)
    assert _evaluate(models).state == "matched"


@pytest.mark.parametrize(
    "moves",
    [
        [(100, 110), (100, 90)],
        [(101, 111.1), (147, 132.3)],
        [(100, 101), (100, 102), (100, 97)],
        [(600, 601), (600, 601), (600, 598)],
    ],
    ids=["integer-prices", "decimal-prices", "three-days", "recurring-ratios"],
)
@pytest.mark.parametrize("direction", [-1, 0, 1])
def test_offsetting_down_market_moves_are_not_positive_resilience(moves, direction):
    # Every sequence has exactly zero mean; change the last quote by one ULP for the controls.
    own = dict.fromkeys(DATES, 100.0)
    benchmark = dict(own)
    for i, (start, end) in enumerate(moves, 1):
        if direction and i == len(moves):
            end = nextafter(end, inf if direction > 0 else 0.0)
        own[DATES[2 * i - 1]], own[DATES[2 * i]] = start, end
        benchmark[DATES[2 * i]] = 99.0
    window = observe(own, benchmark)
    down = window["benchmarks"]["1321"]["down"]
    assert down["days"] == len(moves)
    assert down["mean_excess_return"] > 0
    value = down["own_mean_return"]
    assert (value > 0) - (value < 0) == direction

    models = _example()
    models[1].payload["relative"]["market_conditioned_60d"] = window
    assert _evaluate(models).state == ("matched" if direction > 0 else "not_matched")
