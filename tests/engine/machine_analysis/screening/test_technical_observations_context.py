"""技術合致の真偽だけでなく規模・窓・条件日数を初回比較まで残す。"""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_financial_growth import models, payloads
from tests.engine.machine_analysis.screening.test_technical_financial import row
from tests.engine.machine_analysis.screening.test_technical_strategy import (
    _conditioned,
    _strategy_models,
)
from tests.tools.test_technical_discovery import _metadata

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies.technical_discovery import (
    evaluate_technical_strategy,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    evaluate_technical_financial_rows,
    project_technical_financial_row,
)


def project(technical):
    return project_technical_financial_row(
        "1234", models=(*technical, *models(payloads())), metadata=_metadata()
    )


def brief(actual):
    result = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(), "T", "G", (actual,), ("1234",), {}
    )
    return technical_financial_screening_brief(result)["cards"][0]


@pytest.mark.parametrize("value", [-0.3, -0.001, 0.0, 0.2, None])
def test_numeric_conditions_reach_card_without_changing_routes_or_financial_hypothesis(value):
    technical = _strategy_models()
    technical[0].payload["price_path"]["observations"]["R20"]["value"] = value
    original = deepcopy(technical)
    owner = evaluate_technical_strategy("1234", models=technical, metadata=_metadata())
    actual = project(technical)
    assert actual.technical["observations"] == owner["observations"]
    assert actual.technical["routes"] == owner["routes"]
    assert actual.technical["matched_routes"] == owner["matched_routes"]
    assert actual.financial_gate == project(_strategy_models()).financial_gate
    assert actual.screening_lane == "growth_research"
    card = brief(actual)
    assert card["technical"]["observations"] == owner["observations"]
    assert card["technical"]["observations"]["R20"]["value"] == value
    assert "supporting_context" not in card["technical"]
    card["technical"]["observations"]["R20"]["window"].clear()
    assert actual.technical["observations"] == owner["observations"]
    assert technical == original


@pytest.mark.parametrize("days", [1, 25, 0, None])
def test_market_down_mean_keeps_its_denominator_not_whole_window_strength(days):
    technical = _strategy_models()
    _conditioned(technical)["benchmarks"]["1321"]["down"].update(
        days=days, own_mean_return=-0.004, mean_excess_return=0.008
    )
    actual = project(technical)
    observations = brief(actual)["technical"]["observations"]
    own = observations["market_down_own_return_60d"]
    excess = observations["market_down_excess_return_60d"]
    assert own["value"] == -0.004
    assert excess["value"] == 0.008
    assert own["condition_days"] == excess["condition_days"] == days
    assert own["benchmark_ticker"] == "1321"
    assert own["window"]["calendar_differences"] == 60
    assert own["window"]["window_start_date"] == "2026-08-14"
    assert own["window"]["window_end_date"] == "2026-11-13"
    assert actual.technical["routes"]["market_down_resilience"]["state"] == (
        "not_matched" if days else "unavailable"
    )


def test_condition_frequency_survives_saved_row_and_initial_comparison():
    technical = _strategy_models()
    baseline = project(technical)
    _conditioned(technical)["benchmarks"]["1321"]["down"].update(own_up_days=1, outperform_days=25)
    actual = project(technical)
    codec = TypeAdapter(type(actual))
    restored = codec.validate_json(codec.dump_json(actual))
    observations = brief(restored)["technical"]["observations"]
    assert observations["market_down_own_return_60d"]["condition_counts"] == {
        "own_up_days": 1,
        "outperform_days": 25,
    }
    assert actual.technical["routes"] == baseline.technical["routes"]
    assert actual.financial_gate == baseline.financial_gate
    assert actual.screening_lane == baseline.screening_lane


def test_endpoint_strength_keeps_drawdown_and_concentration_in_saved_card():
    technical = _strategy_models()
    path = technical[0].payload["price_path"]["observations"]["G60"]
    path.update(value=-0.12, window_return=0.06, observed_peak_date="2026-09-18")
    path["path_statistics"].update(
        max_close_drawdown=-0.17,
        largest_absolute_log_change_share=0.7,
        price_row_basis="observed_finite_positive_adjusted_close",
    )
    original = deepcopy(technical)
    actual = project(technical)
    codec = TypeAdapter(type(actual))
    restored = codec.validate_json(codec.dump_json(actual))
    card = brief(restored)
    context = card["technical"]["price_path_60d"]
    value = context["value"]
    assert context["source_state"] == "recorded"
    assert context["source_field"] == "price_path.observations.G60"
    assert value["value"] == -0.12
    assert value["window_return"] == 0.06
    assert value["window_start_date"] == path["window_start_date"]
    assert value["window_end_date"] == "2026-11-13"
    assert value["path_statistics"]["max_close_drawdown"] == -0.17
    assert value["path_statistics"]["largest_absolute_log_change_share"] == 0.7
    assert value["path_statistics"]["price_row_basis"] == "observed_finite_positive_adjusted_close"
    assert "comparison_end_price" not in value
    assert "drawdown_episodes" not in value["path_statistics"]
    value["path_statistics"]["max_close_drawdown"] = 0
    assert technical == original
    assert restored.technical == actual.technical


@pytest.mark.parametrize("state", ["missing", "null", "zero", "incomplete"])
def test_path_shape_preserves_absence_zero_and_unavailability(state):
    technical = _strategy_models()
    observations = technical[0].payload["price_path"]["observations"]
    if state == "missing":
        del observations["G60"]
    elif state == "null":
        observations["G60"] = None
    elif state == "zero":
        observations["G60"]["path_statistics"]["max_close_drawdown"] = 0.0
    else:
        observations["G60"]["path_statistics"] = {
            "status": "incomplete_price_window",
            "efficiency_unavailable_reason": None,
            "max_close_drawdown": None,
        }
    context = brief(project(technical))["technical"]["price_path_60d"]
    if state in {"missing", "null"}:
        assert (
            context["source_state"] == {"missing": "not_recorded", "null": "recorded_null"}[state]
        )
        assert context["value"] is None
    elif state == "zero":
        assert context["value"]["path_statistics"]["max_close_drawdown"] == 0.0
    else:
        assert context["value"]["path_statistics"] == observations["G60"]["path_statistics"]


@pytest.mark.parametrize("case", ["stale", "unrecorded", "model_not_generated"])
def test_value_and_unavailable_reason_stay_together(case):
    technical = _strategy_models()
    if case == "stale":
        technical[0].payload["price_path"]["observations"]["R20"]["window_end_date"] = "2026-11-12"
    elif case == "unrecorded":
        del technical[0].payload["price_path"]["observations"]["R20"]
    else:
        technical = technical[1:]
    actual = project(technical)
    obs = brief(actual)["technical"]["observations"]["R20"]
    assert obs["value"] == (0.1 if case == "stale" else None)
    assert (
        obs["unavailable_reason"]
        == {
            "stale": "price_date_mismatch_or_unknown",
            "unrecorded": "not_recorded",
            "model_not_generated": "model_not_generated",
        }[case]
    )
    assert "R20_positive" in actual.technical["routes"]["trend_continuation"]["missing_conditions"]
    assert actual.screening_lane == "growth_research"


@pytest.mark.parametrize("version", range(1, 13))
def test_old_policy_keeps_absence_and_canonical_bytes_without_regeneration(version):
    legacy = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method=f"technical-financial-discovery-v{version}"),
        "T",
        "G",
        (row("1234"),),
        ("1234",),
        {},
    )
    codec = TypeAdapter(TechnicalFinancialSelection)
    raw = codec.dump_json(legacy)
    restored = codec.validate_json(raw)
    assert codec.dump_json(restored) == raw
    assert (
        "price_path_60d"
        not in technical_financial_screening_brief(restored)["cards"][0]["technical"]
    )
    assert (
        "observations" not in technical_financial_screening_brief(restored)["cards"][0]["technical"]
    )
    with pytest.raises(ValueError, match=r"historical.*read-only"):
        evaluate_technical_financial_rows(_metadata(), tickers=(), rows={}, policy=restored.policy)
