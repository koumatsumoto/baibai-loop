"""戦略入口の意味・時点・不足を検証する。利益効果のtestではない。"""

from copy import deepcopy

import pytest
from tests.tools.test_technical_discovery import _metadata, _models
from tools.experiments import technical_discovery as research_cli

from baibai_engine.machine_analysis.screening.policies.technical_discovery import (
    RESEARCH_POLICY_ID,
    STRATEGY_POLICY_ID,
    evaluate_technical_discovery,
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


def _evaluate(models=None):
    return evaluate_technical_strategy(
        "1234", models=_strategy_models() if models is None else models, metadata=_metadata()
    )


def _conditioned(models):
    return models[1].payload["relative"]["market_conditioned_60d"]


def test_new_strategy_keeps_independent_routes_clocks_and_immutable_inputs():
    models = _strategy_models()
    original = deepcopy(models)
    result = _evaluate(models)
    assert result["policy_id"] == STRATEGY_POLICY_ID
    assert result["kind"] == "technical_discovery_strategy"
    assert result["state"] == "matched"
    assert len(result["routes"]) == 7
    assert {"market_down_resilience", "price_strength_with_activity"} <= set(
        result["matched_routes"]
    )
    assert result["as_of"] == "2026-11-14"
    assert result["expected_price_date"] == "2026-11-13"
    down = result["observations"]["market_down_own_return_60d"]
    assert down["benchmark_ticker"] == "1321"
    assert down["condition_days"] == 25
    assert down["window"]["window_start_date"] == "2026-08-14"
    assert down["window"]["window_end_date"] == "2026-11-13"
    assert "cross_date" not in result["observations"]["sma_75bar_gap"]
    assert not {"rank", "score", "buy", "sell", "target_price"} & result.keys()
    assert models == original


def test_context_keeps_shape_and_turnover_without_synthesizing_joint_event():
    models = _strategy_models()
    result = _evaluate(models)
    support = result["supporting_context"]
    assert support["market_conditioned_60d"]["value"] == _conditioned(models)
    assert (
        support["price_path_60d"]["value"] == models[0].payload["price_path"]["observations"]["G60"]
    )
    support["market_conditioned_60d"]["value"]["benchmarks"]["1321"]["turnover"]["down"][
        "mean_yen"
    ] = 0.0
    assert _conditioned(models)["benchmarks"]["1321"]["turnover"]["down"]["mean_yen"] == 1_000_000.0
    assert not any(
        "turnover" in key for key in result["routes"]["market_down_resilience"]["conditions"]
    )


def test_research_api_is_same_owner_and_old_output_does_not_gain_new_rules():
    assert research_cli.evaluate_technical_discovery is evaluate_technical_discovery
    original = evaluate_technical_discovery("1234", models=_models(), metadata=_metadata())
    new = evaluate_technical_discovery("1234", models=_strategy_models(), metadata=_metadata())
    assert original == new
    assert original["policy_id"] == RESEARCH_POLICY_ID == research_cli.POLICY_ID
    assert len(original["routes"]) == 5
    assert "state" not in original


def test_old_fields_stay_unrecorded_not_filled_by_sma50_or_plain_activity():
    result = _evaluate(_models())
    assert result["observations"]["sma_75bar_gap"]["source_state"] == "not_recorded"
    assert result["routes"]["price_strength_with_activity"]["state"] == "unavailable"
    assert result["routes"]["market_down_resilience"]["state"] == "unavailable"
    assert result["supporting_context"]["market_conditioned_60d"]["source_state"] == "not_recorded"
    assert result["state"] == "matched"


@pytest.mark.parametrize(("own", "excess"), [(0.0, 0.01), (-0.001, 0.01), (0.001, 0.0)])
def test_outperformance_is_not_absolute_upward_resilience(own, excess):
    models = _strategy_models()
    _conditioned(models)["benchmarks"]["1321"]["down"].update(
        own_mean_return=own, mean_excess_return=excess
    )
    assert _evaluate(models)["routes"]["market_down_resilience"]["state"] == "not_matched"


def test_configured_benchmark_not_replaced_by_more_favorable_index():
    models = _strategy_models()
    models[1].payload["price_path_relative"]["benchmark_ticker"] = "1306"
    result = _evaluate(models)
    assert result["observations"]["market_down_own_return_60d"]["benchmark_ticker"] == "1306"
    assert result["routes"]["market_down_resilience"]["state"] == "not_matched"
    assert result["routes"]["price_strength_with_activity"]["state"] == "matched"


@pytest.mark.parametrize("benchmark", [None, "", "  ", 1321, "unknown-index"])
def test_missing_or_unsupported_index_does_not_borrow_data(benchmark):
    models = _strategy_models()
    models[1].payload["price_path_relative"]["benchmark_ticker"] = benchmark
    result = _evaluate(models)
    assert result["routes"]["market_down_resilience"]["state"] == "unavailable"
    assert result["routes"]["price_strength_with_activity"]["state"] == "matched"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("window_end_date", "2026-11-12", "price_date_mismatch_or_unknown"),
        ("window_start_date", None, "window_missing_or_invalid"),
        ("calendar_differences", 20, "condition_window_mismatch_or_unknown"),
        ("window_basis", "observed_rows", "condition_window_mismatch_or_unknown"),
        ("price_basis", "raw_close", "price_basis_mismatch_or_unknown"),
    ],
)
def test_conditioned_observation_requires_saved_period_and_basis(field, value, reason):
    models = _strategy_models()
    _conditioned(models)[field] = value
    result = _evaluate(models)
    assert result["routes"]["market_down_resilience"]["state"] == "unavailable"
    assert result["observations"]["market_down_own_return_60d"]["unavailable_reason"] == reason


@pytest.mark.parametrize("case", ["status", "reason_unrecorded", "days_zero", "days_missing"])
def test_parent_status_and_condition_days_not_ignored(case):
    models = _strategy_models()
    benchmark = _conditioned(models)["benchmarks"]["1321"]
    if case == "status":
        benchmark.update(status="unavailable", unavailable_reason="incomplete_price_window")
    elif case == "reason_unrecorded":
        del benchmark["unavailable_reason"]
    else:
        benchmark["down"]["days"] = 0 if case == "days_zero" else None
    assert _evaluate(models)["routes"]["market_down_resilience"]["state"] == "unavailable"


@pytest.mark.parametrize("name", ["R1", "sma_75bar", "sma_200bar", "activity"])
def test_confirmation_requires_price_above_both_ma_current_rise_and_activity(name):
    models = _strategy_models()
    if name == "R1":
        models[0].payload["price_path"]["observations"]["R1"]["value"] = 0.0
    elif name == "activity":
        models[1].payload["price"]["turnover_activity_20d"]["current_to_prior_mean_ratio"] = 1.0
    else:
        models[0].payload["moving_averages"][name]["gap_ratio"] = 0.0
    assert _evaluate(models)["routes"]["price_strength_with_activity"]["state"] == "not_matched"


def test_stale_price_never_matches_positive_new_observations():
    models = _strategy_models()
    models[0].payload["price_path"]["freshness"] = "stale"
    result = _evaluate(models)
    assert result["state"] == "unavailable"
    assert result["matched_routes"] == []
    assert all(route["state"] == "unavailable" for route in result["routes"].values())


def test_total_state_separates_known_nonmatch_from_unavailable():
    models = _strategy_models()
    models[0].payload["price_momentum"]["value"] = -0.1
    models[0].payload["price_path"]["observations"]["R1"]["value"] = -0.01
    models[1].payload["price_path_relative"]["comparisons"]["R20"]["relative_return"] = -0.1
    models[1].payload["price"]["turnover_activity_20d"]["current_to_prior_mean_ratio"] = 0.5
    _conditioned(models)["benchmarks"]["1321"]["down"].update(
        own_mean_return=-0.02, mean_excess_return=-0.01
    )
    assert _evaluate(models)["state"] == "not_matched"
    del models[1].payload["relative"]["market_conditioned_60d"]
    assert _evaluate(models)["state"] == "unavailable"


def test_turnover_missing_does_not_erase_valid_conditioned_price():
    models = _strategy_models()
    down = _conditioned(models)["benchmarks"]["1321"]["turnover"]["down"]
    down.update(
        status="unavailable", unavailable_reason="missing_or_invalid_turnover", mean_yen=None
    )
    result = _evaluate(models)
    assert result["routes"]["market_down_resilience"]["state"] == "matched"
    saved = result["supporting_context"]["market_conditioned_60d"]["value"]
    assert (
        saved["benchmarks"]["1321"]["turnover"]["down"]["unavailable_reason"]
        == "missing_or_invalid_turnover"
    )
