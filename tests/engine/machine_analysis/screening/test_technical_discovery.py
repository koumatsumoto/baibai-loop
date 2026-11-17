"""現行TA入口の意味・欠測をownerで確認する。利益効果のtestではない。"""

import json
from copy import deepcopy

import pytest
from tests.helpers.technical_discovery import _DAY, _metadata, _models

from baibai_engine.machine_analysis.screening.policies.technical_discovery import (
    evaluate_technical_strategy,
)


def _evaluate(models=None):
    return evaluate_technical_strategy(
        "1234", models=_models() if models is None else models, metadata=_metadata()
    )


def test_independent_routes_use_window_return_not_peak_drawdown_and_keep_source_clock():
    models = _models()
    before = deepcopy(models)
    row = _evaluate(models)
    assert row["matched_routes"] == [
        "market_outperformance",
        "trend_continuation",
        "turnover_expansion",
    ]
    assert row["observations"]["window_return_60d"]["value"] == 0.15
    assert row["observations"]["window_return_60d"]["source_field"].endswith("G60.window_return")
    assert row["routes"]["rebound_start"]["state"] == "not_matched"
    assert row["as_of"] == "2026-11-14"
    assert row["expected_price_date"] == _DAY
    assert row["market_relative_benchmark"] == "1321"
    assert row["observations"]["momentum_12_1"]["window"]["end_date"] == "2026-10-14"
    assert "rank" not in row
    assert "score" not in row
    assert models == before


def test_rebound_relative_resilience_and_pullback_do_not_require_all_routes():
    models = _models()
    models[0].payload["price_path"]["observations"]["R20"]["value"] = -0.1
    models[0].payload["moving_averages"]["sma_20bar"]["gap_ratio"] = -0.01
    models[1].payload["price_path_relative"]["comparisons"]["R20"]["own_return"] = -0.1
    row = _evaluate(models)
    assert "trend_continuation" not in row["matched_routes"]
    assert {"market_outperformance", "trend_pullback", "rebound_start"} <= set(
        row["matched_routes"]
    )
    assert row["observations"]["R20"]["value"] < 0


@pytest.mark.parametrize("invalid", ["foreign_subject", "duplicate_model"])
def test_pure_api_rejects_mixed_subjects_and_duplicate_inputs(invalid):
    models = _models()
    if invalid == "foreign_subject":
        models = (models[0].model_copy(update={"subject": "5678"}), models[1])
    else:
        models = (*models, models[0])
    with pytest.raises(ValueError, match="technical discovery requires"):
        _evaluate(models)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("current_row_state", "no_trade"),
        ("freshness", "stale"),
        ("latest_observed_price_date", "2026-11-12"),
        ("expected_price_date", "2026-11-16"),
        ("current_price", 0),
        ("price_basis", "raw_close"),
    ],
)
def test_retained_strong_indicators_never_match_without_current_price_context(field, value):
    models = _models()
    models[0].payload["price_path"][field] = value
    row = _evaluate(models)
    assert row["matched_routes"] == []
    assert {route["state"] for route in row["routes"].values()} == {"unavailable"}
    assert row["price_context"]["ready"] is False


@pytest.mark.parametrize(
    ("mutation", "expected_state"),
    [
        ("absent", "not_recorded"),
        ("null", "recorded_null"),
        ("model_missing", "model_not_generated"),
    ],
)
def test_unrecorded_null_and_missing_model_remain_distinct(mutation, expected_state):
    models = _models()
    if mutation == "absent":
        del models[0].payload["price_momentum"]
    elif mutation == "null":
        models[0].payload["price_momentum"] = None
    else:
        models = models[1:]
    row = _evaluate(models)
    assert row["observations"]["momentum_12_1"]["source_state"] == expected_state
    assert row["routes"]["trend_continuation"]["state"] == "unavailable"
    assert ("technical_analysis" in row["missing_models"]) == (mutation == "model_missing")


def test_failure_does_not_erase_missing_condition_and_zero_is_not_missing():
    models = _models()
    models[0].payload["price_path"]["observations"]["R20"]["value"] = 0
    models[0].payload["price_momentum"]["value"] = None
    row = _evaluate(models)
    route = row["routes"]["trend_continuation"]
    assert route["state"] == "not_matched"
    assert route["failed_conditions"] == ["R20_positive"]
    assert route["missing_conditions"] == ["momentum_12_1_positive"]
    assert row["observations"]["R20"]["value"] == 0
    assert row["observations"]["R20"]["unavailable_reason"] is None


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("comparison_start_date", "2026-10-14", "comparison_window_mismatch"),
        ("comparison_end_date", "2026-11-12", "price_date_mismatch_or_unknown"),
    ],
)
def test_market_relative_requires_the_same_r20_endpoints(field, value, reason):
    models = _models()
    models[1].payload["price_path_relative"]["comparisons"]["R20"][field] = value
    row = _evaluate(models)
    assert row["routes"]["market_outperformance"]["state"] == "unavailable"
    assert row["observations"]["market_relative_20d"]["unavailable_reason"] == reason
    assert row["routes"]["trend_continuation"]["state"] == "matched"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "0.2"])
def test_invalid_values_are_unavailable_not_positive_votes(value):
    models = _models()
    models[0].payload["price_momentum"]["value"] = value
    row = _evaluate(models)
    assert row["observations"]["momentum_12_1"]["unavailable_reason"] == "nonfinite_or_non_numeric"
    assert row["routes"]["trend_continuation"]["state"] == "unavailable"
    json.dumps(row, allow_nan=False)


def test_activity_never_substitutes_share_volume_current_inclusive_mean_or_missing_source():
    models = _models()
    activity = models[1].payload["price"]["turnover_activity_20d"]
    activity["window_basis"] = "observed_20bar_including_current"
    row = _evaluate(models)
    assert row["routes"]["turnover_expansion"]["state"] == "unavailable"
    assert (
        row["observations"]["turnover_activity_20d"]["unavailable_reason"]
        == "turnover_basis_mismatch_or_unknown"
    )
    del models[1].payload["price"]["turnover_activity_20d"]
    models[1].payload["price"]["avg_turnover_20d_oku"] = 99
    assert _evaluate(models)["routes"]["turnover_expansion"]["state"] == "unavailable"
