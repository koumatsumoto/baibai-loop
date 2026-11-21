"""4経済経路の値・時点・比較可能性を検証する。古い説明カードを再生成しない。"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic import TypeAdapter
from tests.helpers.external_assessment_observations import valuation
from tests.helpers.model_generation import materialize_generation, publish_generation
from tests.helpers.retained_economic_selection import retained_economic_result

from baibai_engine.machine_analysis.generation.assembly import CompanyModelInputs, generate_models
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    GenerationSubject,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.models.company_financials.earnings import (
    EarningsPrice,
    ProfitComponent,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.machine_analysis.models.company_growth.models import (
    Change,
    CompanyGrowth,
    InputRelease,
    Observation,
    PeriodState,
    Value,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies import economic_path_inputs
from baibai_engine.machine_analysis.screening.policies.discovery import read_discovery_observations
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathSelection
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import canonical_json


def metadata():
    return GenerationMetadata(
        generation_id="economic-fixed",
        as_of=date(2026, 1, 5),
        evaluation_time=datetime(2026, 1, 5, 12, tzinfo=UTC),
        inputs={"market": InputSnapshot(kind="lake_release", reference="fixed", sha256="a" * 64)},
    )


def profit_component(coefficient, amount, start, end):
    return ProfitComponent(
        coefficient=coefficient,
        amount_yen=amount,
        period_start=date.fromisoformat(start),
        period_end=date.fromisoformat(end),
        disclosed_at=date(2026, 1, 5),
        disclosed_time=None,
        disclosure_number="synthetic-profit",
        document_type="FinancialStatements_Consolidated_JP",
    )


def payloads(price=100.0):
    cap = price * 100

    def observation(kind, start):
        return Observation(
            measurement=kind,
            period_start=date.fromisoformat(start),
            period_end=date(2025, 12, 31),
            statement_basis="consolidated",
            accounting_standard="JP",
            metrics={
                "sales": Value(value=1000.0),
                "operating_profit": Value(value=100.0),
                "operating_margin": Value(value=0.1),
            },
        )

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
                Change(
                    measurement=kind,
                    metric=metric,
                    from_start=date.fromisoformat(prior),
                    from_end=date(2024, 12, 31),
                    to_start=date.fromisoformat(start),
                    to_end=date(2025, 12, 31),
                    difference=difference,
                    rate=rate,
                )
            )
    return {
        "company_financials": {
            "expected_price_date": "2026-01-05",
            "earnings_price": TypeAdapter(EarningsPrice).dump_python(
                EarningsPrice(
                    as_of=date(2026, 1, 5),
                    earnings_yield=1000.0 / cap,
                    price_date=date(2026, 1, 5),
                    price_yen=price,
                    shares_ex_treasury=100.0,
                    market_cap_yen=cap,
                    profit_ttm_yen=1000.0,
                    statement_basis="consolidated",
                    accounting_standard="JP",
                    profit_components=(profit_component(1, 1000.0, "2025-01-01", "2025-12-31"),),
                    unavailable_reason=None,
                ),
                mode="json",
            ),
            "financial": {"market_cap": cap, "pbr": cap / 20000.0, "fcf_ttm": -200.0},
        },
        "company_growth": CompanyGrowth(
            ticker="1234",
            status="available",
            requested_start=None,
            requested_end=date(2026, 1, 5),
            periods=(
                PeriodState(
                    fiscal_period="FY",
                    fiscal_year_end=date(2025, 12, 31),
                    period_start=date(2025, 1, 1),
                    period_end=date(2025, 12, 31),
                    observations=(
                        observation("annual_ttm", "2025-01-01"),
                        observation("quarter", "2025-10-01"),
                    ),
                ),
            ),
            changes=tuple(changes),
        ).model_dump(mode="json"),
        "market_context": {"master": {"sector_33": "情報・通信業", "name": "合成企業"}},
    }


def models(data):
    return tuple(
        GeneratedModel(model_id=key, subject="1234", calculation_revision="v1", payload=value)
        for key, value in data.items()
    )


def project(data):
    return read_discovery_observations("1234", models=models(data), metadata=metadata())[2]


def active(row, name):
    return next((path for path in row.paths if path.path_id == name and path.eligible), None)


def test_valid_paths_use_fixed_observations_and_periods():
    result = project(payloads())
    expected = {
        "earning_power": (0.1, 0.1),
        "business_expansion": (0.1, 0.2),
        "operating_recovery": (0.001, 0.02),
        "capital_productivity": (2.0, 0.05),
    }
    observed = {p.path_id: (p.price_value, p.business_value) for p in result.paths if p.eligible}
    assert observed == expected
    assert active(result, "business_expansion").variant == "quarter"


def test_growth_policy_bounds_history_without_changing_coordinates_or_saved_payload(monkeypatch):
    data = payloads()
    growth = data["company_growth"]
    latest = growth["periods"][-1]
    history = []
    for year in (2022, 2024, 2024):
        state = deepcopy(latest)
        for key in ("fiscal_year_end", "period_start", "period_end"):
            state[key] = state[key].replace("2025", str(year))
        for item in state["observations"]:
            for key in ("period_start", "period_end"):
                item[key] = item[key].replace("2025", str(year))
        history.append(state)
    history[-1]["fiscal_year_end"] = "2025-03-31"
    growth["periods"] = [*history, latest]
    old_changes = deepcopy(growth["changes"])
    for change in old_changes:
        change["to_start"] = change["to_start"].replace("2025", "2022")
        change["to_end"] = change["to_end"].replace("2025", "2022")
    duplicate = deepcopy(growth["changes"][1])
    duplicate.pop("margin_points")
    growth["changes"] = [*old_changes, *growth["changes"], duplicate]
    before = canonical_json(data)

    bounded = economic_path_inputs._growth_policy_inputs(growth)
    assert bounded["periods"] == [*history[1:], latest]
    assert len(bounded["changes"]) == 7
    assert bounded["changes"][-1] is duplicate
    assert all(
        bounded[key] is value for key, value in growth.items() if key not in {"periods", "changes"}
    )
    result = project(data)
    assert not any(
        p.eligible
        for p in result.paths
        if (p.path_id, p.variant) == ("operating_recovery", "quarter")
    )
    monkeypatch.setattr(economic_path_inputs, "_growth_policy_inputs", lambda payload: payload)
    assert project(data) == result
    assert canonical_json(data) == before


def test_retained_v4_selection_has_its_original_canonical_hash_and_absences():
    selected = retained_economic_result()
    retained = selected.cards[0].context
    assert retained["capital_action_context"] == {
        "observations": [],
        "share_basis_adjustments": [],
        "capital_basis_reason": None,
    }
    saved = TypeAdapter(EconomicPathSelection).dump_python(selected, mode="json")
    assert "turnover_window" not in saved["cards"][0]["context"]
    # Fixed saved bytes from before window projection; no historical rank recomputation.
    assert sha256(canonical_json(saved).encode()).hexdigest() == (
        "31dfe3c282d5aced68d54872768986538e0d4a3b358213529e9297c82c78adeb"
    )
    assert "moving_averages" not in saved["cards"][0]["context"]
    assert "price_path" not in saved["cards"][0]["context"]


def test_price_change_moves_price_axes_not_business_axes():
    before, after = project(payloads()), project(payloads(price=200.0))
    for name in {path.path_id for path in before.paths}:
        a, b = active(before, name), active(after, name)
        assert b.price_value == pytest.approx(a.price_value / 2)
        assert b.business_value == pytest.approx(a.business_value)


def test_missing_earnings_and_negative_cash_do_not_remove_growth_or_recovery():
    data = payloads()
    data["company_financials"]["earnings_price"]["earnings_yield"] = None
    data["company_financials"]["earnings_price"]["unavailable_reason"] = "unavailable_annual_profit"
    result = project(data)
    assert active(result, "earning_power") is None
    assert active(result, "capital_productivity") is None
    assert active(result, "business_expansion") is not None
    assert active(result, "operating_recovery") is not None


def test_stale_price_disables_price_coordinates_but_preserves_business_data():
    data = payloads()
    data["company_financials"]["expected_price_date"] = "2026-01-06"
    result = project(data)
    assert not any(path.eligible for path in result.paths)
    assert any(path.business_value is not None for path in result.paths)
    assert all(path.price_value is None for path in result.paths)


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


def test_zero_quarter_sales_comparison_does_not_use_positive_annual():
    data = payloads()
    for observation in data["company_growth"]["periods"][0]["observations"]:
        if observation["measurement"] == "quarter":
            observation["metrics"]["sales"]["value"] = 0.0
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter" and change["metric"] == "sales":
            change.update(difference=0.0, rate=None, reason="nonpositive_base")
    result = project(data)
    assert active(result, "business_expansion") is None
    assert active(result, "operating_recovery") is not None


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
    data["company_financials"]["earnings_price"][field] = value
    result = project(data)
    assert not any(path.eligible for path in result.paths)
    assert all(path.price_value is None for path in result.paths)
    assert (
        next(path for path in result.paths if path.path_id == "earning_power").business_value == 0.1
    )


def test_income_uses_actual_composite_ttm_interval_not_earliest_component_start():
    data = payloads()
    data["company_financials"]["earnings_price"]["profit_components"] = TypeAdapter(
        tuple[ProfitComponent, ...]
    ).dump_python(
        (
            profit_component(1, 500.0, "2025-04-01", "2025-09-30"),
            profit_component(1, 900.0, "2024-04-01", "2025-03-31"),
            profit_component(-1, 400.0, "2024-04-01", "2024-09-30"),
        ),
        mode="json",
    )
    data["company_growth"]["periods"][0]["period_end"] = "2025-09-30"
    annual = data["company_growth"]["periods"][0]["observations"][0]
    annual.update(period_start="2024-10-01", period_end="2025-09-30")
    data["company_growth"]["periods"][0]["observations"][1].update(
        period_start="2025-07-01",
        period_end="2025-09-30",
    )
    income = active(project(data), "earning_power")
    assert (income.price_value, income.business_value) == (0.1, 0.1)
    annual["period_start"] = "2024-04-01"
    result = project(data)
    assert active(result, "earning_power") is None
    assert next(path for path in result.paths if path.path_id == "earning_power").price_value == 0.1


@pytest.mark.parametrize(
    ("field", "profit_basis", "growth_basis"),
    [
        ("statement_basis", None, "unknown"),
        ("statement_basis", "unknown", "unknown"),
        ("statement_basis", "nonconsolidated", "consolidated"),
        ("accounting_standard", "IFRS", "JP"),
        ("accounting_standard", None, None),
    ],
)
def test_income_requires_known_matching_financial_bases(field, profit_basis, growth_basis):
    data = payloads()
    data["company_financials"]["earnings_price"][field] = profit_basis
    data["company_growth"]["periods"][0]["observations"][0][field] = growth_basis
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
        PeriodState(
            fiscal_period="1Q",
            fiscal_year_end=date(2026, 12, 31),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
            observations=(),
        ).model_dump(mode="json")
    )
    result = project(data)
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
    data["company_financials"]["earnings_price"]["earnings_yield"] = -0.1
    data["company_financials"]["earnings_price"]["profit_components"][0]["amount_yen"] = -1000.0
    result = project(data)
    assert active(result, "earning_power") is None
    assert active(result, "capital_productivity") is None
    capital = next(path for path in result.paths if path.path_id == "capital_productivity")
    assert capital.business_value == pytest.approx(-0.05)
    assert active(result, "business_expansion") is not None


def test_overflowing_derived_coordinates_are_missing_not_nonfinite():
    data = payloads()
    earnings = data["company_financials"]["earnings_price"]
    earnings.update(
        price_yen=1e-308, shares_ex_treasury=1.0, market_cap_yen=1e-308, earnings_yield=1e308
    )
    data["company_financials"]["financial"].update(market_cap=1e-308, pbr=1e308)
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


def test_new_or_unbuilt_models_do_not_cast_votes_or_create_coordinates():
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
    after = read_discovery_observations("1234", models=models(data), metadata=planned)[2]
    assert after.paths == before.paths


@pytest.mark.parametrize("missing_shares", [False, True])
def test_foreign_provider_or_later_forecast_does_not_change_yen_coordinates(missing_shares):
    data = payloads()
    if missing_shares:
        data["company_financials"]["earnings_price"].update(
            shares_ex_treasury=None, market_cap_yen=None
        )
    before = project(data)
    later = metadata().evaluation_time + timedelta(microseconds=1)
    foreign = valuation(later, currency="USD", consensus_target=999999.0)
    data["external_assessment"] = {
        "model_valuations": [
            {
                "inputs": {"latest": foreign.model_dump(mode="json"), "previous": None},
                "comparisons": [{"metric": "consensus_target", "state": "first_observation"}],
            }
        ]
    }
    data["company_financials"].update(
        provider_capital_observation={
            "currency": "USD",
            "price_date": "2026-05-08",
            "close_yen": 1e8,
            "market_cap_million_yen": 1e8,
        },
        holder_capital_evidence={"observations": [{"total_stocks": 2500000}]},
    )
    data["company_financials"]["financial"].update(
        forecast_eps_yen=999999.0, forecast_profit_yen=1e12
    )
    source = canonical_json(data)
    after = project(data)
    assert after.paths == before.paths
    assert canonical_json(data) == source
    if missing_shares:
        assert not any(p.eligible or p.price_value is not None for p in after.paths)


def test_capital_proxy_uses_pbr_not_unselected_equity_or_edinet_scope():
    data = payloads()
    data["company_financials"]["financial"].update(
        same_state_equity_yen=999999.0, consolidation_basis="standalone"
    )
    capital = active(project(data), "capital_productivity")
    assert capital.business_value == pytest.approx(0.05)
    data["company_financials"]["financial"]["market_cap"] = 999.0
    assert active(project(data), "capital_productivity") is None


def test_economic_result_roundtrip_and_history_identity(tmp_path):
    header = metadata()
    generation = ModelGeneration(**header.model_dump(), models=models(payloads()))
    runs = tmp_path / "runs.sqlite"
    publish_generation(generation, path=runs)
    result = ScreeningSelection(header.generation_id, ("1234",), retained_economic_result())
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
    assert active(result, "operating_recovery").variant == "annual_ttm"
    quarter = next(
        p for p in result.paths if (p.path_id, p.variant) == ("operating_recovery", "quarter")
    )
    assert not quarter.eligible
    assert quarter.price_value is None
    assert active(result, "business_expansion") is not None
    assert active(result, "earning_power") is not None


def test_different_saved_comparison_shapes_remain_ambiguous_even_when_typed_values_match():
    data = payloads()
    duplicate = deepcopy(data["company_growth"]["changes"][1])
    duplicate.pop("margin_points")
    data["company_growth"]["changes"].append(duplicate)
    result = project(data)
    assert active(result, "operating_recovery").variant == "annual_ttm"
    assert active(result, "business_expansion") is not None


def test_missing_comparison_field_does_not_create_a_match_from_the_owner_default():
    data = payloads()
    before = project(data)
    duplicate = deepcopy(data["company_growth"]["changes"][1])
    duplicate.pop("comparison")
    data["company_growth"]["changes"].append(duplicate)
    after = project(data)
    assert after == before
    data["company_growth"]["changes"][1].pop("comparison")
    result = project(data)
    assert active(result, "operating_recovery").variant == "annual_ttm"
    assert active(result, "business_expansion").variant == "quarter"


@pytest.mark.parametrize("metric", ["operating_profit", "operating_margin"])
@pytest.mark.parametrize("bad_value", [-1.0, 0.0])
def test_bad_quarter_with_missing_companion_cannot_escape_to_annual(metric, bad_value):
    data = payloads()
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter" and change["metric"] != "sales":
            change["difference"] = bad_value if change["metric"] == metric else None
    result = project(data)
    assert active(result, "operating_recovery") is None


def test_positive_quarter_with_missing_companion_uses_only_available_annual_comparison():
    data = payloads()
    for change in data["company_growth"]["changes"]:
        if change["measurement"] == "quarter" and change["metric"] == "operating_margin":
            change["difference"] = None
    result = project(data)
    assert active(result, "operating_recovery").variant == "annual_ttm"
    assert active(result, "operating_recovery").business_value == 0.02


def owner_generation(
    *, alternate_quarter_profit=None, alternate_prior_quarter_profit=None, reverse_documents=False
):
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
    if alternate_prior_quarter_profit is not None:
        documents.append(
            replace(
                documents[0],
                fiscal_year_end=date(2026, 6, 30),
                operating_profit=alternate_prior_quarter_profit,
                disclosure_number="owner-prior-fy-end-change",
            )
        )
    if reverse_documents:
        documents.reverse()
    stream = generate_models(
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
    )
    return materialize_generation(stream)


def test_owner_fy_and_cumulative_quarter_reach_economic_coordinates():
    generation = owner_generation()
    row = read_discovery_observations("1001", models=generation.models, metadata=generation)[2]
    paths = {item.path_id: item for item in row.paths if item.eligible}
    assert set(paths) >= {"earning_power", "business_expansion", "operating_recovery"}
    income = paths["earning_power"]
    assert income.price_value == pytest.approx(110.0 / 800.0)
    assert income.business_value == pytest.approx(120.0 / 1350.0)
    expansion = paths["business_expansion"]
    assert expansion.variant == "quarter"
    assert expansion.price_value == pytest.approx(1350.0 / 800.0)
    assert expansion.business_value == pytest.approx(0.6)
    recovery = paths["operating_recovery"]
    assert recovery.price_value == pytest.approx(20.0 / 800.0)
    assert recovery.business_value == pytest.approx(30.0 / 400.0 - 10.0 / 250.0)


def test_native_fiscal_year_end_tie_uses_owner_last_anchor_and_local_ambiguity():
    generation = owner_generation(alternate_quarter_profit=50.0)
    growth = generation.result("company_growth", "1001").payload
    assert growth["periods"][-1]["fiscal_year_end"] == "2027-06-30"
    row = read_discovery_observations("1001", models=generation.models, metadata=generation)[2]
    quarter = next(
        item
        for item in row.paths
        if item.path_id == "operating_recovery" and item.variant == "quarter"
    )
    assert not quarter.eligible
    assert quarter.price_value is None
    assert active(row, "business_expansion").business_value == pytest.approx(0.6)
    fixed_quarter = next(
        x for x in growth["periods"][-1]["observations"] if x["measurement"] == "quarter"
    )
    assert fixed_quarter["metrics"]["operating_profit"]["value"] == 50.0


@pytest.mark.parametrize("alternate_prior_profit", [10.0, 50.0])
def test_prior_native_period_tie_keeps_same_values_but_cannot_invent_recovery(
    alternate_prior_profit,
):
    generation = owner_generation(alternate_prior_quarter_profit=alternate_prior_profit)
    reversed_generation = owner_generation(
        alternate_prior_quarter_profit=alternate_prior_profit, reverse_documents=True
    )
    growth_model = generation.result("company_growth", "1001")
    assert growth_model == reversed_generation.result("company_growth", "1001")
    assert growth_model.calculation_revision == "company_growth_retrospective_v22"
    growth = growth_model.payload
    before = canonical_json(growth)
    priors = [state for state in growth["periods"] if state["period_end"] == "2025-06-30"]
    assert [state["fiscal_year_end"] for state in priors] == ["2026-03-31", "2026-06-30"]
    quarters = [
        next(item for item in state["observations"] if item["measurement"] == "quarter")
        for state in priors
    ]
    assert [item["metrics"]["operating_profit"]["value"] for item in quarters] == [
        10.0,
        alternate_prior_profit,
    ]
    assert [
        item["metrics"]["operating_profit"]["documents"][0]["disclosure_number"]
        for item in quarters
    ] == ["owner-2025-1Q", "owner-prior-fy-end-change"]
    row = read_discovery_observations("1001", models=generation.models, metadata=generation)[2]
    reversed_row = read_discovery_observations(
        "1001", models=reversed_generation.models, metadata=reversed_generation
    )[2]
    assert row == reversed_row
    # The same reported-period ends but different FYs do not create another vote.
    assert active(row, "business_expansion").business_value == pytest.approx(0.6)
    recovery = active(row, "operating_recovery")
    if alternate_prior_profit == 50.0:
        assert recovery is None
    else:
        assert recovery.price_value == pytest.approx(20.0 / 800.0)
        assert recovery.business_value == pytest.approx(30.0 / 400.0 - 10.0 / 250.0)
    assert canonical_json(growth) == before
