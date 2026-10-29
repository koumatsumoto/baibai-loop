from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from pydantic import TypeAdapter

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    GenerationSubject,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
    project_company,
)
from baibai_engine.machine_analysis.screening.policies.economic_path_selection import (
    select_saved_economic_generation,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


def metadata():
    return GenerationMetadata(
        generation_id="economic-fixed",
        as_of=date(2026, 1, 5),
        evaluation_time=datetime(2026, 1, 5, 12, tzinfo=UTC),
        inputs={"market": InputSnapshot(kind="lake_release", reference="fixed", sha256="a" * 64)},
    )


def payloads(price=100.0):
    cap = price * 100

    def observation(kind, start):
        return {
            "measurement": kind,
            "period_start": start,
            "period_end": "2025-12-31",
            "statement_basis": "consolidated",
            "metrics": {
                "sales": {"value": 1000.0},
                "operating_profit": {"value": 100.0},
                "operating_margin": {"value": 0.1},
            },
        }

    changes = []
    for kind, start, prior in [
        ("quarter", "2025-10-01", "2024-10-01"),
        ("annual_ttm", "2025-01-01", "2024-01-01"),
    ]:
        for metric, difference, rate in [
            ("sales", 100.0, 0.2),
            ("operating_profit", 10.0, 0.1),
            ("operating_margin", 0.02, None),
        ]:
            changes.append(
                {
                    "measurement": kind,
                    "metric": metric,
                    "comparison": "year_over_year",
                    "from_start": prior,
                    "from_end": "2024-12-31",
                    "to_start": start,
                    "to_end": "2025-12-31",
                    "difference": difference,
                    "rate": rate,
                    "reason": None,
                }
            )
    return {
        "company_valuation": {
            "expected_price_date": "2026-01-05",
            "earnings_price": {
                "earnings_yield": 1000.0 / cap,
                "price_date": "2026-01-05",
                "price_yen": price,
                "shares_ex_treasury": 100.0,
                "market_cap_yen": cap,
                "statement_basis": "consolidated",
                "profit_components": [
                    {
                        "coefficient": 1,
                        "amount_yen": 1000.0,
                        "period_start": "2025-01-01",
                        "period_end": "2025-12-31",
                    }
                ],
            },
            "financial": {"market_cap": cap, "pbr": cap / 20000.0, "fcf_ttm": -200.0},
        },
        "company_growth": {
            "periods": [
                {
                    "period_end": "2025-12-31",
                    "observations": [
                        observation("annual_ttm", "2025-01-01"),
                        observation("quarter", "2025-10-01"),
                    ],
                }
            ],
            "changes": changes,
        },
        "market_context": {"master": {"sector_33": "情報・通信業", "name": "合成企業"}},
    }


def models(data):
    return tuple(
        GeneratedModel(model_id=key, subject="1234", calculation_revision="v1", payload=value)
        for key, value in data.items()
    )


def project(data):
    return project_company("1234", models=models(data), metadata=metadata())


def active(row, name):
    return next((path for path in row.paths if path.path_id == name and path.eligible), None)


def test_valid_paths_use_fixed_observations_and_periods():
    result = project(payloads())
    assert {path.path_id for path in result.paths if path.eligible} == {
        "earning_power",
        "business_expansion",
        "operating_recovery",
        "capital_productivity",
    }
    assert active(result, "earning_power").business_value == 0.1
    assert active(result, "business_expansion").variant == "quarter"
    assert active(result, "operating_recovery").business_value == 0.02


def test_price_change_moves_price_axes_not_business_axes():
    before, after = project(payloads()), project(payloads(price=200.0))
    for name in {path.path_id for path in before.paths}:
        a, b = active(before, name), active(after, name)
        assert b.price_value == pytest.approx(a.price_value / 2)
        assert b.business_value == pytest.approx(a.business_value)


def test_missing_earnings_and_negative_cash_do_not_remove_growth_or_recovery():
    data = payloads()
    data["company_valuation"]["earnings_price"]["earnings_yield"] = None
    data["company_valuation"]["earnings_price"]["unavailable_reason"] = "unavailable_annual_profit"
    result = project(data)
    assert active(result, "earning_power") is None
    assert active(result, "capital_productivity") is None
    assert active(result, "business_expansion") is not None
    assert active(result, "operating_recovery") is not None


def test_stale_price_disables_price_coordinates_but_preserves_business_data():
    data = payloads()
    data["company_valuation"]["expected_price_date"] = "2026-01-06"
    result = project(data)
    assert not any(path.eligible for path in result.paths)
    assert any(path.business_value is not None for path in result.paths)
    assert result.context["financial"]["fcf_ttm"] == -200.0


@pytest.mark.parametrize("difference,rate", [(-1.0, -0.1), (0.0, 0.0)])
def test_latest_nonpositive_quarter_does_not_choose_positive_annual(difference, rate):
    data = payloads()
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter":
            change["difference"] = difference
            change["rate"] = rate if change["metric"] == "sales" else None
    result = project(data)
    assert active(result, "business_expansion") is None
    assert active(result, "operating_recovery") is None
    assert all(
        not path.eligible
        for path in result.paths
        if path.variant == "annual_ttm"
        and path.path_id in {"business_expansion", "operating_recovery"}
    )


def test_missing_quarter_uses_same_anchor_annual_not_old_good_quarter():
    data = payloads()
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter":
            change["difference"] = None
            change["rate"] = None
    old = deepcopy(data["company_growth"]["changes"][0])
    old.update(to_end="2025-09-30", to_start="2025-07-01", rate=100.0)
    data["company_growth"]["changes"].append(old)
    result = project(data)
    assert active(result, "business_expansion").variant == "annual_ttm"
    assert active(result, "business_expansion").business_value == 0.2
    assert active(result, "operating_recovery").variant == "annual_ttm"
    assert result.context["latest_growth_changes"]["sales"]["variant"] == "annual_ttm"


def test_loss_narrowing_difference_remains_valid_with_nonpositive_base():
    data = payloads()
    for observation in data["company_growth"]["periods"][0]["observations"]:
        observation["metrics"]["operating_profit"]["value"] = -40.0
        observation["metrics"]["operating_margin"]["value"] = -0.04
    for change in data["company_growth"]["changes"]:
        if change["metric"] != "sales":
            change["reason"] = "nonpositive_base"
    result = project(data)
    assert active(result, "earning_power") is None
    assert active(result, "operating_recovery") is not None
    assert active(result, "business_expansion") is not None


def test_financial_margin_inapplicability_is_not_recomputed_from_raw_amounts():
    data = payloads()
    data["market_context"]["master"]["sector_33"] = "銀行業"
    for observation in data["company_growth"]["periods"][0]["observations"]:
        observation["metrics"]["operating_margin"] = {
            "value": None,
            "reason": "not_applicable_to_financial_sector",
        }
    for change in data["company_growth"]["changes"]:
        if change["metric"] == "operating_margin":
            change["difference"] = None
    result = project(data)
    assert active(result, "earning_power") is None
    assert active(result, "operating_recovery") is None
    assert active(result, "capital_productivity") is not None


@pytest.mark.parametrize("sector", [None, "0", "", "その他"])
def test_unknown_sector_is_not_an_industry_peer_group(sector):
    data = payloads()
    data["market_context"]["master"]["sector_33"] = sector
    result = project(data)
    assert not any(path.eligible for path in result.paths)
    income = next(path for path in result.paths if path.path_id == "earning_power")
    assert income.price_value == 0.1
    assert income.business_value is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("price_yen", 0.0),
        ("price_yen", -1.0),
        ("price_yen", None),
        ("shares_ex_treasury", 0.0),
        ("shares_ex_treasury", -1.0),
        ("shares_ex_treasury", None),
        ("market_cap_yen", 0.0),
        ("market_cap_yen", -1.0),
        ("market_cap_yen", None),
        ("price_date", None),
    ],
)
def test_price_gate_requires_all_positive_price_inputs_on_expected_date(field, value):
    data = payloads()
    data["company_valuation"]["earnings_price"][field] = value
    result = project(data)
    assert not result.context["pricing_valid"]
    assert not any(path.eligible for path in result.paths)
    assert all(path.price_value is None for path in result.paths)
    assert (
        next(path for path in result.paths if path.path_id == "earning_power").business_value == 0.1
    )


def test_income_uses_actual_composite_ttm_interval_not_earliest_component_start():
    data = payloads()
    data["company_valuation"]["earnings_price"]["profit_components"] = [
        {
            "coefficient": 1,
            "amount_yen": 500.0,
            "period_start": "2025-04-01",
            "period_end": "2025-09-30",
        },
        {
            "coefficient": 1,
            "amount_yen": 900.0,
            "period_start": "2024-04-01",
            "period_end": "2025-03-31",
        },
        {
            "coefficient": -1,
            "amount_yen": 400.0,
            "period_start": "2024-04-01",
            "period_end": "2024-09-30",
        },
    ]
    data["company_growth"]["periods"][0]["period_end"] = "2025-09-30"
    annual = data["company_growth"]["periods"][0]["observations"][0]
    annual.update(period_start="2024-10-01", period_end="2025-09-30")
    data["company_growth"]["periods"][0]["observations"][1].update(
        period_start="2025-07-01",
        period_end="2025-09-30",
    )
    income = active(project(data), "earning_power")
    assert income is not None
    assert income.evidence["earnings_period_start"] == "2024-10-01"
    assert income.evidence["earnings_period_end"] == "2025-09-30"
    annual["period_start"] = "2024-04-01"
    result = project(data)
    assert active(result, "earning_power") is None
    assert next(path for path in result.paths if path.path_id == "earning_power").price_value == 0.1


@pytest.mark.parametrize("basis", [None, "unknown", "nonconsolidated"])
def test_income_requires_observed_matching_statement_basis(basis):
    data = payloads()
    data["company_valuation"]["earnings_price"]["statement_basis"] = basis
    if basis != "nonconsolidated":
        data["company_growth"]["periods"][0]["observations"][0]["statement_basis"] = basis
    result = project(data)
    assert active(result, "earning_power") is None
    assert active(result, "business_expansion") is not None
    assert active(result, "capital_productivity") is not None


def test_period_variants_retain_both_reference_coordinates_before_selection():
    result = project(payloads())
    for name in ("business_expansion", "operating_recovery"):
        variants = {path.variant: path for path in result.paths if path.path_id == name}
        assert set(variants) == {"quarter", "annual_ttm"}
        assert variants["quarter"].eligible
        assert not variants["annual_ttm"].eligible
        assert variants["annual_ttm"].business_value is not None
        assert variants["annual_ttm"].price_value is not None
    expansion = [path for path in result.paths if path.path_id == "business_expansion"]
    assert expansion[0].price_group == expansion[1].price_group
    assert expansion[0].price_value == expansion[1].price_value


def test_newest_anchor_does_not_search_older_complete_observations():
    data = payloads()
    data["company_growth"]["periods"].append(
        {
            "period_end": "2026-03-31",
            "observations": [],
        }
    )
    result = project(data)
    assert result.context["latest_growth_period"] == "2026-03-31"
    assert active(result, "earning_power") is None
    assert active(result, "business_expansion") is None
    assert active(result, "operating_recovery") is None
    assert active(result, "capital_productivity") is not None


def test_recovery_requires_both_changes_from_same_comparison_interval():
    data = payloads()
    for change in data["company_growth"]["changes"]:
        if change["metric"] == "operating_margin":
            change["from_start"] = "2024-07-01"
    result = project(data)
    assert active(result, "operating_recovery") is None
    assert active(result, "business_expansion") is not None


def test_negative_reported_profit_is_retained_in_capital_proxy_not_called_roe():
    data = payloads()
    data["company_valuation"]["earnings_price"]["earnings_yield"] = -0.1
    data["company_valuation"]["earnings_price"]["profit_components"][0]["amount_yen"] = -1000.0
    result = project(data)
    assert active(result, "earning_power") is None
    assert active(result, "capital_productivity") is None
    capital = next(path for path in result.paths if path.path_id == "capital_productivity")
    assert capital.business_value == pytest.approx(-0.05)
    assert "not_verified_common_or_average_equity_roe" in capital.evidence["interpretation"]
    assert active(result, "business_expansion") is not None


def test_overflowing_derived_coordinates_are_missing_not_nonfinite():
    data = payloads()
    earnings = data["company_valuation"]["earnings_price"]
    earnings.update(
        price_yen=1e-308, shares_ex_treasury=1.0, market_cap_yen=1e-308, earnings_yield=1e308
    )
    data["company_valuation"]["financial"].update(market_cap=1e-308, pbr=1e308)
    for observation in data["company_growth"]["periods"][0]["observations"]:
        observation["metrics"]["sales"]["value"] = 1e308
    for change in data["company_growth"]["changes"]:
        if change["metric"] == "operating_profit":
            change["difference"] = 1e308
    result = project(data)
    assert active(result, "business_expansion") is None
    assert active(result, "operating_recovery") is None
    assert active(result, "capital_productivity") is None
    assert (
        next(path for path in result.paths if path.path_id == "capital_productivity").business_value
        is None
    )


def test_added_models_are_referenced_without_automatic_extra_coordinates_or_votes():
    data = payloads()
    before = project(data)
    data["new_company_model"] = {"discovery": "detail_only", "score": 100.0}
    planned = metadata().model_copy(
        update={
            "schema_version": 2,
            "subject_plan": {
                "1234": GenerationSubject(
                    scope="stock",
                    model_ids=(*data, "not_generated_model"),
                )
            },
        }
    )
    after = project_company("1234", models=models(data), metadata=planned)
    assert after.paths == before.paths
    assert after.model_revisions["new_company_model"] == "v1"
    assert after.not_generated == ("not_generated_model",)


def test_latest_growth_changes_remain_context_when_improvements_are_not_positive():
    data = payloads()
    data["company_valuation"]["financial"]["tradable_share_change_yoy"] = 0.15
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter":
            change["difference"] = -1.0
            change["rate"] = -0.1 if change["metric"] == "sales" else None
    result = project(data)
    assert active(result, "business_expansion") is None
    assert active(result, "operating_recovery") is None
    latest = result.context["latest_growth_changes"]
    assert latest["sales"]["comparison"]["rate"] == -0.1
    for metric in ("sales", "operating_profit", "operating_margin"):
        assert latest[metric]["variant"] == "quarter"
        assert latest[metric]["comparison"]["difference"] == -1.0
    assert result.context["financial"]["tradable_share_change_yoy"] == 0.15


def test_external_context_preserves_actual_group_comparisons_without_original_snapshots():
    data = payloads()
    before = project(data)
    data["external_assessment"] = {
        "model_valuations": [
            {
                "inputs": {
                    "latest": {
                        "distributor": "valuation_distributor",
                        "per_consensus_origin": "valuation_origin",
                        "per_consensus_period": "FY2026",
                        "currency": "JPY",
                        "snapshot_only_payload": "not_a_summary",
                    },
                    "previous": {
                        "per_consensus_origin": "valuation_origin",
                        "per_consensus_period": "FY2026",
                    },
                },
                "comparisons": [
                    {
                        "metric": "per_consensus",
                        "state": "decreased",
                        "comparable": True,
                        "latest_value": 9.0,
                        "previous_value": 10.0,
                        "delta": -1.0,
                    }
                ],
            }
        ],
        "analyst_ratings": [
            {
                "distributor": "rating_distributor",
                "inputs": {
                    "latest": {
                        "origin": "rating_origin",
                        "rating_scale_min": 1.0,
                        "rating_scale_max": 5.0,
                        "rating_scale_direction": "lower_is_positive",
                    }
                },
                "comparison": {
                    "metric": "rating",
                    "state": "count_changed",
                    "comparable": False,
                    "reason": "count_changed",
                    "latest_count": 2,
                    "previous_count": 1,
                    "delta": None,
                },
            }
        ],
        "quants_scores": [
            {
                "inputs": {
                    "latest": {
                        "distributor": "kabumap",
                        "origin": "Quants Research",
                        "score_as_of_date": "2026-01-05",
                        "score_scale_max": 10,
                    }
                },
                "comparisons": [
                    {
                        "metric": "growth_score",
                        "state": "increased",
                        "comparable": True,
                        "latest_value": 8.0,
                        "previous_value": 7.0,
                        "delta": 1.0,
                    }
                ],
            }
        ],
        "tradingview": {
            "availability": "forecast_fields_present",
            "inputs": {
                "latest": {"target_period_label": "FY2026", "target_period_end": "2026-12-31"}
            },
            "comparisons": [
                {
                    "metric": "eps_forecast_next_fy",
                    "state": "not_comparable",
                    "comparable": False,
                    "reason": "metric_period_mapping_unconfirmed",
                    "latest_value": 12.0,
                    "previous_value": 10.0,
                    "delta": None,
                }
            ],
        },
    }
    result = project(data)
    assert result.paths == before.paths
    external = result.context["external_assessment"]
    summaries = {item["kind"]: item for item in external["comparisons"]}
    assert set(summaries) == {"model_valuations", "analyst_ratings", "quants_scores", "tradingview"}
    valuation = summaries["model_valuations"]
    assert valuation["origin"] == "valuation_origin"
    assert valuation["target_period"] == valuation["previous_target_period"] == "FY2026"
    assert valuation["delta"] == -1.0
    assert summaries["analyst_ratings"]["state"] == "count_changed"
    assert summaries["analyst_ratings"]["delta"] is None
    assert summaries["analyst_ratings"]["basis"]["rating_scale_direction"] == "lower_is_positive"
    assert summaries["quants_scores"]["delta"] == 1.0
    assert summaries["tradingview"]["state"] == "not_comparable"
    assert summaries["tradingview"]["delta"] is None
    assert summaries["tradingview"]["target_period"] is None
    assert summaries["tradingview"]["source_target_period_label"] == "FY2026"
    assert "snapshot_only_payload" not in str(external)


@pytest.mark.parametrize(
    "state", ["first_observation", "recovered", "count_changed", "not_comparable"]
)
def test_external_noncomparison_states_are_not_promoted_to_revisions(state):
    data = payloads()
    data["external_assessment"] = {
        "analyst_ratings": [
            {
                "distributor": "received_rating",
                "comparison": {
                    "metric": "rating",
                    "state": state,
                    "comparable": False,
                    "latest_value": 4.0,
                    "delta": None,
                },
            }
        ]
    }
    summary = project(data).context["external_assessment"]["comparisons"][0]
    assert summary["state"] == state
    assert summary["comparable"] is False
    assert summary["delta"] is None
    assert "revision" not in summary


def test_capital_proxy_uses_pbr_not_unselected_equity_or_edinet_scope():
    data = payloads()
    data["company_valuation"]["financial"].update(
        same_state_equity_yen=999999.0, consolidation_basis="standalone"
    )
    capital = active(project(data), "capital_productivity")
    assert capital.business_value == pytest.approx(0.05)
    assert capital.evidence["book_amount_implied_by_pbr"] == 20000.0
    assert "not_exposed" in capital.evidence["book_provenance"]
    data["company_valuation"]["financial"]["market_cap"] = 999.0
    assert active(project(data), "capital_productivity") is None


def test_saved_new_result_roundtrip_and_history_identity(tmp_path):
    header = metadata()
    generation = ModelGeneration(**header.model_dump(), models=models(payloads()))
    runs = tmp_path / "runs.sqlite"
    ModelGenerationStore(runs).publish(generation)
    result = select_saved_economic_generation(
        header.generation_id, tickers=("1234",), policy=EconomicPathPolicy(), path=runs
    )
    store = ScreeningSelectionStore(runs)
    assert store.publish("policy-new", result)
    assert not store.publish("policy-new", result)
    assert store.read("policy-new") == result
    codec = TypeAdapter(ScreeningSelection)
    assert codec.validate_json(codec.dump_json(result)) == result
