"""新発見入口が使う価格の鮮度・観測期間・ゼロと未観測を区別する。"""

import pytest
from tests.engine.machine_analysis.screening.test_technical_strategy import _evaluate, _example
from tests.helpers.technical_discovery import _DAY, _models


@pytest.mark.parametrize(
    ("scope", "field", "value"),
    [
        ("path", "current_row_state", "no_trade"),
        ("path", "freshness", "stale"),
        ("path", "latest_observed_price_date", "2026-11-12"),
        ("path", "expected_price_date", "not-a-date"),
        ("path", "expected_price_date", None),
        ("path", "current_price", 0),
        ("path", "current_price", -1),
        ("path", "price_basis", "raw_close"),
        ("technical", "price_basis", "raw_close"),
        ("master", "is_common_stock", False),
    ],
)
def test_retained_indicators_cannot_bypass_current_common_stock_price(scope, field, value):
    models = _models()
    ta, market = (m.payload for m in models)
    {"path": ta["price_path"], "technical": ta, "master": market["master"]}[scope][field] = value
    result = _evaluate(models)
    assert result.state == "unavailable"
    assert not result.price_ready
    assert result.expected_price_date == (value if field == "expected_price_date" else _DAY)


def test_future_price_is_rejected_even_when_observed_and_expected_agree():
    models = _models()
    models[0].payload["price_path"].update(
        expected_price_date="2026-11-16", latest_observed_price_date="2026-11-16"
    )
    result = _evaluate(models)
    assert result.expected_price_date == "2026-11-16"
    assert not result.price_ready
    assert result.state == "unavailable"


def test_absent_models_do_not_supply_price_or_claim_a_negative_discovery():
    result = _evaluate(())
    assert result.expected_price_date is None
    assert not result.price_ready
    assert result.state == "unavailable"


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
    [("comparison_start_date", "2026-10-14"), ("comparison_end_date", "2026-11-12")],
)
def test_comparison_with_different_20_day_endpoints_is_not_a_relative_match(field, value):
    models = _example("outperformance")
    models[1].payload["price_path_relative"]["comparisons"]["R20"][field] = value
    assert _evaluate(models).state == "unavailable"


def test_window_return_not_peak_drawdown_drives_continuation_and_zero_can_disqualify_it():
    models = _example("continuation")
    obs = models[0].payload["price_path"]["observations"]
    assert obs["G60"]["value"] < 0 < obs["G60"]["window_return"]
    assert _evaluate(models).state == "matched"
    obs["G60"]["window_return"] = None
    assert _evaluate(models).state == "unavailable"
    obs["R20"]["value"] = 0
    assert _evaluate(models).state == "not_matched"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "0.2"])
def test_non_numeric_or_nonfinite_price_momentum_cannot_create_a_match(value):
    models = _example("continuation")
    models[0].payload["price_momentum"]["value"] = value
    assert _evaluate(models).state == "unavailable"


@pytest.mark.parametrize(
    ("field", "value", "state"),
    [
        ("basis", "share_volume", "unavailable"),
        ("window_basis", "observed_20bar_including_current", "unavailable"),
        ("current_date", "2026-11-12", "unavailable"),
        ("current_to_prior_mean_ratio", 1.0, "not_matched"),
        ("current_to_prior_mean_ratio", 0.0, "not_matched"),
        ("current_to_prior_mean_ratio", None, "unavailable"),
    ],
)
def test_turnover_requires_comparable_yen_volume_against_the_prior_excluding_current(
    field, value, state
):
    models = _example("turnover")
    models[1].payload["price"]["turnover_activity_20d"][field] = value
    result = _evaluate(models)
    assert result.price_ready
    assert result.state == state
