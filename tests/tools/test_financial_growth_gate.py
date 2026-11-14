from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from tools.experiments.financial_growth_gate import evaluate_financial_growth_gate

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    InputSnapshot,
)
from baibai_engine.machine_analysis.models.company_growth.models import (
    Change,
    CompanyGrowth,
    Observation,
    PeriodState,
    SourceDocument,
    Value,
)
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import project_company


def metadata():
    return GenerationMetadata(
        generation_id="synthetic-fixed-generation",
        as_of=date(2026, 11, 13),
        evaluation_time=datetime(2026, 11, 14, 8, 41, tzinfo=UTC),
        inputs={"market": InputSnapshot(kind="lake_release", reference="fixed", sha256="a" * 64)},
    )


def observation(measurement, start, end, metrics):
    return Observation(
        period_start=date.fromisoformat(start),
        period_end=date.fromisoformat(end),
        measurement=measurement,
        statement_basis="consolidated",
        accounting_standard="JP",
        period_status="available",
        metrics={
            name: Value(
                value=value,
                documents=(
                    SourceDocument(
                        disclosure_number="synthetic",
                        disclosed_at=date(2026, 10, 30),
                        disclosed_time="15:00:00",
                        document_type="FinancialStatements_Consolidated_JP",
                    ),
                ),
            )
            for name, value in metrics.items()
        },
    )


def payloads():
    metrics = {"sales": 1000.0, "operating_profit": 100.0, "operating_margin": 0.1}
    growth = CompanyGrowth(
        ticker="1234",
        status="available",
        requested_start=None,
        requested_end=date(2026, 11, 13),
        periods=(
            PeriodState(
                fiscal_period="3Q",
                fiscal_year_end=date(2026, 12, 31),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 9, 30),
                observations=(
                    observation("quarter", "2026-07-01", "2026-09-30", metrics),
                    observation("annual_ttm", "2025-10-01", "2026-09-30", metrics),
                    observation("reported", "2026-01-01", "2026-09-30", metrics),
                    observation("balance", "2026-09-30", "2026-09-30", {"net_assets": 500.0}),
                ),
            ),
        ),
        changes=tuple(
            Change(
                metric=metric,
                measurement="quarter",
                comparison="year_over_year",
                from_start=date(2025, 7, 1),
                from_end=date(2025, 9, 30),
                to_start=date(2026, 7, 1),
                to_end=date(2026, 9, 30),
                from_value=before,
                to_value=after,
                difference=after - before,
            )
            for metric, before, after in (
                ("sales", 900.0, 1000.0),
                ("operating_profit", 80.0, 100.0),
                ("operating_margin", 0.08, 0.1),
            )
        ),
    ).model_dump(mode="json")
    return {
        "company_growth": growth,
        "company_financials": {
            "financial": {"cash": 50.0, "debt": None, "fcf_ttm": -20.0},
        },
    }


def models(data):
    return tuple(
        GeneratedModel(model_id=key, subject="1234", calculation_revision="test-v1", payload=value)
        for key, value in data.items()
    )


def evaluate(data):
    return evaluate_financial_growth_gate("1234", models=models(data), metadata=metadata())


def current(data, measurement):
    return next(
        item
        for item in data["company_growth"]["periods"][-1]["observations"]
        if item["measurement"] == measurement
    )


def change(data, metric):
    return next(item for item in data["company_growth"]["changes"] if item["metric"] == metric)


def add_prior_fy(data):
    values = {"sales": 3000.0, "operating_profit": 500.0, "operating_margin": 1 / 6}
    prior = PeriodState(
        fiscal_period="FY",
        fiscal_year_end=date(2025, 12, 31),
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        observations=(
            observation("reported", "2025-01-01", "2025-12-31", values),
            observation("annual_ttm", "2025-01-01", "2025-12-31", values),
        ),
    ).model_dump(mode="json")
    data["company_growth"]["periods"].insert(0, prior)
    annual = current(data, "annual_ttm")
    annual["period_status"] = "matching_period_not_observed"
    for item in annual["metrics"].values():
        item.update(value=None, reason="matching_period_not_observed")


def test_research_routes_are_explicit_unadopted_and_preserve_clock_and_null():
    result = evaluate(payloads())
    assert result["state"] == "research_candidate"
    assert result["matched_routes"] == ["profit_growth", "operating_recovery"]
    assert result["adoption_status"] == "unadopted_research_only"
    assert result["generation_id"] == metadata().generation_id
    assert result["as_of"] == "2026-11-13"
    assert result["evaluation_time"] == "2026-11-14T08:41:00+00:00"
    assert result["financial_context"]["debt"] is None
    assert result["financial_context"]["fcf_ttm"] == -20.0
    assert "action" not in result
    assert result["observations"]["comparison_basis"] == "source_reported_economic_scope_unverified"


def test_price_sector_technical_and_old_eligibility_do_not_decide_financial_gate():
    data = payloads()
    baseline = evaluate(data)
    assert not any(
        path.eligible
        for path in project_company("1234", models=models(data), metadata=metadata()).paths
    )
    data["company_financials"].update(
        expected_price_date=None, earnings_price={"invalid": "unused pricing"}
    )
    data["market_context"] = {"master": {"sector_33": "その他"}}
    data["technical_analysis"] = {"arbitrary_score": -999999}
    assert evaluate(data) == baseline


@pytest.mark.parametrize("profit", [-10.0, 0.0])
def test_loss_narrowing_or_breakeven_can_be_research_recovery_not_normal_profit(profit):
    data = payloads()
    current(data, "annual_ttm")["metrics"]["operating_profit"]["value"] = -80.0
    current(data, "quarter")["metrics"]["operating_profit"]["value"] = profit
    current(data, "quarter")["metrics"]["operating_margin"]["value"] = profit / 1000
    change(data, "operating_profit").update(
        from_value=-30.0,
        to_value=profit,
        difference=profit + 30,
        rate=None,
        reason="nonpositive_base",
    )
    change(data, "operating_margin").update(
        from_value=-0.03, to_value=profit / 1000, difference=profit / 1000 + 0.03
    )
    result = evaluate(data)
    assert result["matched_routes"] == ["operating_recovery"]
    assert result["routes"]["profit_growth"]["state"] == "not_matched"
    assert result["checks"]["quarter_operating_profit_increase"]["reason"] == "nonpositive_base"


def test_nonpositive_base_keeps_positive_sales_difference_but_not_worsening_loss():
    data = payloads()
    current(data, "annual_ttm")["metrics"]["operating_profit"]["value"] = -80.0
    current(data, "quarter")["metrics"]["operating_profit"]["value"] = -33.0
    change(data, "sales").update(
        from_value=0.0, to_value=25.0, difference=25.0, rate=None, reason="nonpositive_base"
    )
    change(data, "operating_profit").update(
        from_value=-30.0, to_value=-33.0, difference=-3.0, rate=None, reason="nonpositive_base"
    )
    change(data, "operating_margin").update(difference=None, reason="operand_unavailable")
    result = evaluate(data)
    assert result["state"] == "not_selected"
    assert result["checks"]["quarter_sales_increase"]["state"] == "met"
    assert result["checks"]["quarter_operating_profit_increase"]["state"] == "not_met"
    assert "quarter_operating_margin_increase" in result["routes"]["operating_recovery"]["missing"]


def test_missing_quarter_comparison_is_not_replaced_by_positive_cumulative_growth():
    data = payloads()
    for item in data["company_growth"]["changes"]:
        item.update(difference=None, reason="operand_unavailable")
    cumulative = deepcopy(change(data, "sales"))
    cumulative.update(
        measurement="reported",
        to_start="2026-01-01",
        from_start="2025-01-01",
        difference=1000.0,
        reason=None,
    )
    data["company_growth"]["changes"].append(cumulative)
    result = evaluate(data)
    assert result["state"] == "input_unresolved"
    assert result["checks"]["quarter_sales_increase"]["value"] is None
    assert result["checks"]["quarter_sales_increase"]["reason"] == "operand_unavailable"


def test_known_failure_and_missing_are_both_retained():
    data = payloads()
    current(data, "balance")["metrics"]["net_assets"]["value"] = -1.0
    change(data, "operating_profit").update(difference=None, reason="operand_unavailable")
    result = evaluate(data)
    assert result["state"] == "not_selected"
    for route in result["routes"].values():
        assert route["failures"] == ["positive_net_assets"]
        assert "quarter_operating_profit_increase" in route["missing"]


def test_missing_financial_model_is_not_zero_debt_or_negative_equity():
    data = payloads()
    del data["company_financials"]
    result = evaluate(data)
    assert result["state"] == "research_candidate"
    assert result["missing_models"] == ["company_financials"]
    assert result["financial_context"]["debt"] is None
    assert result["checks"]["positive_net_assets"]["value"] == 500.0


def test_financial_sector_inapplicability_is_not_recomputed_or_negative():
    data = payloads()
    for measurement in ("quarter", "annual_ttm"):
        for metric in ("operating_profit", "operating_margin"):
            current(data, measurement)["metrics"][metric].update(
                value=None, reason="not_applicable_to_financial_sector"
            )
    for metric in ("operating_profit", "operating_margin"):
        change(data, metric).update(difference=None, reason="not_applicable_to_financial_sector")
    result = evaluate(data)
    assert result["state"] == "input_unresolved"
    assert (
        result["checks"]["positive_quarter_operating_profit"]["reason"]
        == "not_applicable_to_financial_sector"
    )
    assert not any(route["failures"] for route in result["routes"].values())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("statement_basis", "nonconsolidated"),
        ("accounting_standard", None),
        ("period_end", "2026-06-30"),
    ],
)
def test_balance_basis_or_date_gap_is_unresolved_not_economic_failure(field, value):
    data = payloads()
    current(data, "balance")[field] = value
    result = evaluate(data)
    assert result["state"] == "input_unresolved"
    assert result["checks"]["balance_quarter_basis"]["reason"] == "basis_or_period_unaligned"


def test_different_comparison_intervals_do_not_form_a_growth_or_recovery_pair():
    data = payloads()
    change(data, "operating_profit")["from_start"] = "2025-04-01"
    result = evaluate(data)
    assert result["state"] == "input_unresolved"
    assert result["checks"]["growth_comparison_interval"]["state"] == "unavailable"
    assert result["checks"]["recovery_comparison_interval"]["state"] == "unavailable"


def test_conflicting_comparisons_are_localized_instead_of_choosing_good_one():
    data = payloads()
    duplicate = deepcopy(change(data, "operating_profit"))
    duplicate["difference"] = -50.0
    data["company_growth"]["changes"].append(duplicate)
    result = evaluate(data)
    assert result["state"] == "input_unresolved"
    assert (
        result["checks"]["quarter_operating_profit_increase"]["reason"]
        == "ambiguous_growth_comparison"
    )
    assert result["checks"]["quarter_sales_increase"]["state"] == "met"


def test_limited_full_fy_fallback_keeps_missing_current_ttm_and_source_period():
    data = payloads()
    add_prior_fy(data)
    result = evaluate(data)
    assert "profit_growth" in result["matched_routes"]
    assert result["observations"]["annual_variant"] == "full_fy"
    assert result["observations"]["annual"]["period_end"] == "2025-12-31"
    assert result["observations"]["latest_ttm"]["period_status"] == "matching_period_not_observed"


@pytest.mark.parametrize("mode", ["metric_missing", "basis_change", "undisclosed_fy"])
def test_full_fy_cannot_repair_metric_gap_basis_change_or_later_source(mode):
    data = payloads()
    add_prior_fy(data)
    change(data, "operating_margin")["difference"] = 0.0
    if mode == "metric_missing":
        current(data, "annual_ttm")["period_status"] = "available"
    elif mode == "basis_change":
        current(data, "annual_ttm")["accounting_standard"] = "IFRS"
    else:
        for item in data["company_growth"]["periods"][0]["observations"]:
            item["metrics"]["operating_profit"]["documents"][0]["disclosed_at"] = "2026-11-15"
    result = evaluate(data)
    assert result["state"] == "input_unresolved"
    assert result["checks"]["positive_annual_operating_profit"]["state"] == "unavailable"


def test_newer_income_reversal_prevents_full_fy_restoring_ordinary_route():
    data = payloads()
    add_prior_fy(data)
    loss = deepcopy(current(data, "reported")["metrics"]["operating_profit"])
    loss["value"] = -5.0
    current(data, "reported")["metrics"]["parent_profit"] = loss
    change(data, "operating_margin")["difference"] = 0.0
    result = evaluate(data)
    assert result["state"] == "not_selected"
    assert result["routes"]["profit_growth"]["failures"] == [
        "full_fy_without_newer_income_reversal"
    ]


def test_fy_reversal_check_does_not_use_a_later_disclosure_as_current_evidence():
    data = payloads()
    add_prior_fy(data)
    loss = deepcopy(current(data, "reported")["metrics"]["operating_profit"])
    loss["value"] = -5.0
    loss["documents"][0]["disclosed_at"] = "2026-11-15"
    current(data, "reported")["metrics"]["parent_profit"] = loss
    result = evaluate(data)
    assert "profit_growth" in result["matched_routes"]
    assert result["checks"]["full_fy_without_newer_income_reversal"]["state"] == "met"
    assert result["observations"]["latest_reported"]["metrics"]["parent_profit"]["value"] == -5.0


def test_full_fy_does_not_rescue_observed_latest_deterioration():
    data = payloads()
    add_prior_fy(data)
    change(data, "operating_profit")["difference"] = -10.0
    change(data, "operating_margin").update(difference=None, reason="operand_unavailable")
    result = evaluate(data)
    assert result["state"] == "not_selected"
    assert result["routes"]["operating_recovery"]["missing"] == [
        "quarter_operating_margin_increase"
    ]


@pytest.mark.parametrize("generated", [False, True])
def test_generated_unavailable_is_not_the_same_as_missing_model(generated):
    data = payloads()
    if generated:
        data["company_growth"].update(
            status="unavailable", reason="no_financial_inputs", periods=[], changes=[]
        )
    else:
        del data["company_growth"]
    result = evaluate(data)
    assert result["state"] == "input_unresolved"
    assert ("company_growth" in result["missing_models"]) is not generated
    assert result["observations"]["growth_reason"] == ("no_financial_inputs" if generated else None)


def test_diagnostic_does_not_mutate_saved_inputs_or_return_mutable_aliases():
    data = payloads()
    before = deepcopy(data)
    result = evaluate(data)
    result["observations"]["quarter"]["metrics"]["sales"]["value"] = 999.0
    assert data == before


def test_rejects_cross_subject_and_duplicate_model_input():
    saved = models(payloads())
    with pytest.raises(ValueError, match="matching subject"):
        evaluate_financial_growth_gate("9999", models=saved, metadata=metadata())
    with pytest.raises(ValueError, match="unique saved models"):
        evaluate_financial_growth_gate("1234", models=(*saved, saved[0]), metadata=metadata())


def test_rejects_misattributed_growth_payload():
    data = payloads()
    data["company_growth"]["ticker"] = "9999"
    with pytest.raises(ValueError, match="different subject"):
        evaluate(data)
