from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import TypeAdapter

from baibai_engine.machine_analysis.generation.assembly import CompanyModelInputs, generate_models
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    GenerationSubject,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
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


@pytest.mark.parametrize(("difference", "rate"), [(-1.0, -0.1), (0.0, 0.0)])
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
    assert result.context["latest_growth_changes"]["sales"]["selected_variant"] == "annual_ttm"


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
    ("field", "value"),
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
    assert latest["sales"]["latest_quarter"]["rate"] == -0.1
    for metric in ("sales", "operating_profit", "operating_margin"):
        assert latest[metric]["selected_variant"] == "quarter"
        assert latest[metric]["latest_quarter"]["difference"] == -1.0
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


def test_identical_growth_date_matches_collapse_without_duplicate_votes():
    data = payloads()
    before = project(data)
    data["company_growth"]["changes"] += deepcopy(data["company_growth"]["changes"])
    assert project(data).paths == before.paths


def test_conflicting_growth_date_matches_only_make_that_metric_unavailable():
    data = payloads()
    duplicate = deepcopy(data["company_growth"]["changes"][1])
    duplicate["difference"] = 999.0
    data["company_growth"]["changes"].append(duplicate)
    result = project(data)
    latest = result.context["latest_growth_changes"]
    assert latest["operating_profit"]["latest_quarter"]["reason"] == ("ambiguous_growth_comparison")
    assert latest["operating_margin"]["latest_quarter"]["difference"] == 0.02
    assert active(result, "business_expansion") is not None
    assert active(result, "earning_power") is not None


@pytest.mark.parametrize("metric", ["operating_profit", "operating_margin"])
@pytest.mark.parametrize("bad_value", [-1.0, 0.0])
def test_bad_quarter_with_missing_companion_cannot_escape_to_annual(metric, bad_value):
    data = payloads()
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter" and change["metric"] != "sales":
            change["difference"] = bad_value if change["metric"] == metric else None
    result = project(data)
    assert active(result, "operating_recovery") is None
    latest = result.context["latest_growth_changes"][metric]
    assert latest["selected_variant"] == "quarter"
    assert latest["latest_quarter"]["difference"] == bad_value


def test_positive_quarter_with_missing_companion_retains_counterevidence_on_annual_fallback():
    data = payloads()
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter" and change["metric"] == "operating_margin":
            change["difference"] = None
    result = project(data)
    assert active(result, "operating_recovery").variant == "annual_ttm"
    latest = result.context["latest_growth_changes"]
    assert latest["operating_profit"]["latest_quarter"]["difference"] == 10.0
    assert latest["operating_margin"]["latest_quarter"]["difference"] is None
    assert latest["operating_margin"]["selected_variant"] == "annual_ttm"
    assert latest["operating_margin"]["selected_comparison"]["difference"] == 0.02


def test_generated_unavailable_growth_reason_is_not_not_generated():
    data = payloads()
    data["company_growth"] = {
        "status": "unavailable",
        "reason": "accounting_period_not_observed",
        "periods": [],
    }
    result = project(data)
    assert result.context["growth_status"] == "unavailable"
    assert result.context["growth_reason"] == "accounting_period_not_observed"
    assert "company_growth" in result.model_revisions
    assert "company_growth" not in result.not_generated
    assert "status" not in result.context["external_assessment"]


def owner_generation(*, alternate_quarter_profit=None):
    as_of = date(2026, 8, 31)
    release = InputRelease(release_id="owner-economic-fixture", manifest_sha256="b" * 64)

    def document(*, year, fiscal_period, end, sales, op, profit, disclosed):
        return JQuantsFinancialSummary(
            ticker="1001",
            disclosed_at=disclosed,
            fiscal_period=fiscal_period,
            fiscal_year_end=date(year + 1, 3, 31),
            period_start=date(year, 4, 1),
            period_end=end,
            sales=sales,
            operating_profit=op,
            profit=profit,
            shares_outstanding=100.0,
            treasury_shares=20.0,
            bps=10.0,
            disclosure_number=f"owner-{year}-{fiscal_period}",
            document_type=f"{fiscal_period}FinancialStatements_Consolidated_JP",
        )

    documents = [
        document(
            year=2025,
            fiscal_period="1Q",
            end=date(2025, 6, 30),
            sales=250.0,
            op=10.0,
            profit=15.0,
            disclosed=date(2025, 8, 10),
        ),
        document(
            year=2025,
            fiscal_period="FY",
            end=date(2026, 3, 31),
            sales=1200.0,
            op=100.0,
            profit=100.0,
            disclosed=date(2026, 5, 15),
        ),
        document(
            year=2026,
            fiscal_period="1Q",
            end=date(2026, 6, 30),
            sales=400.0,
            op=30.0,
            profit=25.0,
            disclosed=date(2026, 8, 10),
        ),
    ]
    if alternate_quarter_profit is not None:
        documents.append(
            replace(
                documents[-1],
                fiscal_year_end=date(2027, 6, 30),
                operating_profit=alternate_quarter_profit,
                disclosure_number="owner-fy-end-change",
            )
        )
    return generate_models(
        generation_id="owner-economic-generation",
        as_of=as_of,
        evaluation_time=datetime(2026, 9, 1, tzinfo=UTC),
        inputs={
            "market": InputSnapshot(
                kind="lake_release", reference=release.release_id, sha256=release.manifest_sha256
            )
        },
        financial_release=release,
        companies=(
            CompanyModelInputs(
                ticker="1001",
                financial_documents=documents,
                adjusted_bars=(PriceBar(as_of, 10.0, None),),
                adjustment_events=(),
                sector="情報・通信業",
            ),
        ),
        trading_dates=tuple(as_of - timedelta(days=252 - i) for i in range(253)),
        expected_price_date=as_of,
        ttm_rules=TTMRules(
            full_year_min_days=330,
            full_year_max_days=370,
            period_length_tolerance_ratio=0.1,
            period_end_tolerance_days=45,
        ),
        external_observations={},
        market_context=None,
        securities_context={
            "1001": {"master": {"sector_33": "情報・通信業", "name": "owner fixture"}}
        },
        market_context_revision="owner-fixture-v1",
    ).materialize()


def test_owner_fy_and_cumulative_quarter_flow_through_saved_new_policy(tmp_path):
    generation = owner_generation()
    path = tmp_path / "owner-runs.sqlite"
    ModelGenerationStore(path).publish(generation)
    selection = select_saved_economic_generation(
        generation.generation_id,
        tickers=("1001",),
        policy=EconomicPathPolicy(),
        path=path,
    )
    assert selection.result.tickers == ("1001",)
    card = selection.result.cards[0]
    paths = {item.path_id: item for item in card.paths}
    income = paths["earning_power"]
    assert income.price_value == pytest.approx(110.0 / 800.0)
    assert income.business_value == pytest.approx(120.0 / 1350.0)
    assert income.evidence["same_period_and_statement_basis"] is True
    assert income.evidence["earnings_period_start"] == "2025-07-01"
    assert income.evidence["earnings_period_end"] == "2026-06-30"
    assert income.evidence["business_observation"]["statement_basis"] == "consolidated"
    expansion = paths["business_expansion"]
    assert expansion.variant == "quarter"
    assert expansion.price_value == pytest.approx(1350.0 / 800.0)
    assert expansion.business_value == pytest.approx(0.6)
    recovery = paths["operating_recovery"]
    assert recovery.price_value == pytest.approx(20.0 / 800.0)
    assert recovery.business_value == pytest.approx(30.0 / 400.0 - 10.0 / 250.0)
    assert card.context["growth_status"] == "available"


def test_native_fiscal_year_end_tie_uses_owner_last_anchor_and_local_ambiguity(tmp_path):
    generation = owner_generation(alternate_quarter_profit=50.0)
    growth = generation.result("company_growth", "1001").payload
    assert growth["periods"][-1]["fiscal_year_end"] == "2027-06-30"
    row = project_company("1001", models=generation.models, metadata=generation)
    quarter = next(
        item
        for item in row.paths
        if item.path_id == "operating_recovery" and item.variant == "quarter"
    )
    assert quarter.evidence["operating_observation"]["observation"]["value"] == 50.0
    latest = row.context["latest_growth_changes"]
    assert latest["operating_profit"]["latest_quarter"]["reason"] == ("ambiguous_growth_comparison")
    assert latest["sales"]["latest_quarter"]["rate"] == pytest.approx(0.6)
    path = tmp_path / "native-tie.sqlite"
    ModelGenerationStore(path).publish(generation)
    select_saved_economic_generation(
        generation.generation_id,
        tickers=("1001",),
        policy=EconomicPathPolicy(),
        path=path,
    )


def test_cash_investment_components_and_source_survive_the_same_fcf():
    first = payloads()
    first["company_valuation"]["financial"].update(
        edinet_ocf_ttm=100.0,
        capex_ttm=300.0,
        edinet_capex_source="purchase_of_fixed_assets",
        consolidation_basis="consolidated",
        edinet_failure_reasons="cash_flow_detail_partial",
        edinet_source_period_start="2024-04-01",
        edinet_source_period_end="2025-03-31",
    )
    second = deepcopy(first)
    second["company_valuation"]["financial"].update(edinet_ocf_ttm=-100.0, capex_ttm=100.0)
    a, b = project(first).context["financial"], project(second).context["financial"]
    assert a["fcf_ttm"] == b["fcf_ttm"] == -200.0
    assert a["edinet_ocf_ttm"] == 100.0 and b["edinet_ocf_ttm"] == -100.0
    assert a["capex_ttm"] == 300.0 and b["capex_ttm"] == 100.0
    for field in (
        "edinet_capex_source",
        "consolidation_basis",
        "edinet_failure_reasons",
        "edinet_source_period_start",
        "edinet_source_period_end",
    ):
        assert a[field] == b[field] == first["company_valuation"]["financial"][field]
