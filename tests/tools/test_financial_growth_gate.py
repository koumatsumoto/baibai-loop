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


def mark_financial_margin_inapplicable(data, *, missing_op=False):
    for measurement in ("quarter", "annual_ttm"):
        current(data, measurement)["metrics"]["operating_margin"].update(
            value=None, reason="not_applicable_to_financial_sector"
        )
        if missing_op:
            current(data, measurement)["metrics"]["operating_profit"].update(
                value=None, reason="not_observed"
            )
    change(data, "operating_margin").update(
        difference=None, reason="not_applicable_to_financial_sector"
    )
    if missing_op:
        change(data, "operating_profit").update(difference=None, reason="operand_unavailable")


def test_banking_shape_separates_inapplicability_without_substituting_other_income():
    data = payloads()
    mark_financial_margin_inapplicable(data, missing_op=True)
    baseline = evaluate(data)
    data["company_financials"]["financial"].update(
        operating_profit=999.0, operating_profit_source="ordinary_profit"
    )
    current(data, "quarter")["metrics"]["parent_profit"] = {"value": 1234.0}
    result = evaluate(data)
    assert result["state"] == "input_unresolved"
    assert result["routes"] == baseline["routes"]
    assert result["checks"] == baseline["checks"]
    assert result["routes"]["profit_growth"]["missing_by_kind"]["not_applicable"] == []
    recovery = result["routes"]["operating_recovery"]
    assert recovery["missing_by_kind"]["not_applicable"] == [
        "quarter_operating_margin_observed",
        "quarter_operating_margin_increase",
    ]
    assert recovery["missing_by_kind"]["other_unavailable"] == [
        "quarter_operating_profit_observed",
        "quarter_operating_profit_increase",
    ]
    assert recovery["failures"] == []
    assert recovery["profitability"] is None
    assert result["observations"]["quarter"]["metrics"]["parent_profit"]["value"] == 1234.0


def test_finance_ordinary_growth_is_not_vetoed_by_unused_inapplicable_margin():
    data = payloads()
    mark_financial_margin_inapplicable(data)
    result = evaluate(data)
    assert result["state"] == "research_candidate"
    assert result["matched_routes"] == ["profit_growth"]
    assert all(not group for group in result["routes"]["profit_growth"]["missing_by_kind"].values())
    assert result["routes"]["operating_recovery"]["missing_by_kind"]["not_applicable"]


def test_inapplicability_does_not_rescue_known_negative_equity_or_erase_missing():
    data = payloads()
    mark_financial_margin_inapplicable(data, missing_op=True)
    current(data, "balance")["metrics"]["net_assets"]["value"] = -1.0
    result = evaluate(data)
    assert result["state"] == "not_selected"
    for route in result["routes"].values():
        assert route["failures"] == ["positive_net_assets"]
        assert route["missing"]
    assert result["routes"]["operating_recovery"]["missing_by_kind"]["not_applicable"]


@pytest.mark.parametrize("mode", ["not_generated", "generated_unavailable", "operand_missing"])
def test_missing_is_not_assumed_industry_inapplicability_from_sector_name(mode):
    data = payloads()
    data["market_context"] = {"master": {"sector_33": "銀行業"}}
    if mode == "not_generated":
        del data["company_growth"]
    elif mode == "generated_unavailable":
        data["company_growth"].update(status="unavailable", periods=[], changes=[])
    else:
        change(data, "operating_profit").update(difference=None, reason="operand_unavailable")
    result = evaluate(data)
    assert result["state"] == "input_unresolved"
    assert all(
        not route["missing_by_kind"]["not_applicable"] for route in result["routes"].values()
    )


def test_all_missing_checks_partition_without_changing_reason_or_failure():
    data = payloads()
    mark_financial_margin_inapplicable(data, missing_op=True)
    current(data, "balance")["accounting_standard"] = None
    current(data, "balance")["metrics"]["net_assets"]["value"] = -1.0
    result = evaluate(data)
    for route in result["routes"].values():
        groups = route["missing_by_kind"]
        flat = [name for group in groups.values() for name in group]
        assert sorted(flat) == sorted(route["missing"])
        assert len(flat) == len(set(flat))
        assert groups["unresolved_comparability"] == ["balance_quarter_basis"]
        assert route["failures"] == ["positive_net_assets"]
    assert result["checks"]["balance_quarter_basis"]["reason"] == "basis_or_period_unaligned"
    assert result["checks"]["quarter_operating_profit_observed"]["reason"] == "not_observed"


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("statement_basis_incomparable", "unresolved_comparability"),
        ("accounting_standard_incomparable", "unresolved_comparability"),
        ("operand_unavailable", "other_unavailable"),
        ("comparable_prior_period_not_observed", "other_unavailable"),
        ("future_reason_not_yet_classified", "other_unavailable"),
    ],
)
def test_unavailable_reason_partition_is_narrow_and_unknown_is_not_rewritten(reason, kind):
    data = payloads()
    change(data, "operating_profit").update(difference=None, reason=reason)
    result = evaluate(data)
    assert result["checks"]["quarter_operating_profit_increase"]["reason"] == reason
    for route in result["routes"].values():
        assert "quarter_operating_profit_increase" in route["missing_by_kind"][kind]


def test_nonpositive_margin_denominator_is_neither_sector_na_nor_a_fetch_gap():
    data = payloads()
    current(data, "quarter")["metrics"]["sales"]["value"] = 0.0
    current(data, "quarter")["metrics"]["operating_margin"].update(
        value=None, reason="nonpositive_sales"
    )
    change(data, "operating_margin").update(difference=None, reason="operand_unavailable")
    result = evaluate(data)
    groups = result["routes"]["operating_recovery"]["missing_by_kind"]
    assert groups["not_applicable"] == []
    assert "quarter_operating_margin_observed" in groups["other_unavailable"]
    assert result["checks"]["quarter_operating_margin_observed"]["reason"] == "nonpositive_sales"
    assert result["observations"]["quarter"]["metrics"]["sales"]["value"] == 0.0


@pytest.mark.parametrize(
    ("before", "after", "prior_sign", "current_sign"),
    [
        (-30.0, -10.0, "negative", "negative"),
        (-30.0, 0.0, "negative", "zero"),
        (-30.0, 10.0, "negative", "positive"),
        (0.0, 10.0, "zero", "positive"),
        (10.0, 20.0, "positive", "positive"),
    ],
)
def test_recovery_profitability_does_not_erase_annual_loss_or_funding_gaps(
    before, after, prior_sign, current_sign
):
    data = payloads()
    current(data, "annual_ttm")["metrics"]["operating_profit"]["value"] = -100.0
    current(data, "quarter")["metrics"]["operating_profit"]["value"] = after
    current(data, "quarter")["metrics"]["operating_margin"]["value"] = after / 1000
    change(data, "operating_profit").update(
        from_value=before, to_value=after, difference=after - before
    )
    change(data, "operating_margin").update(
        from_value=before / 900, to_value=after / 1000, difference=after / 1000 - before / 900
    )
    result = evaluate(data)
    assert result["matched_routes"] == ["operating_recovery"]
    assert result["routes"]["operating_recovery"]["profitability"] == {
        "current_quarter": current_sign,
        "comparison_prior_quarter": prior_sign,
    }
    assert "positive_annual_operating_profit" in result["routes"]["profit_growth"]["failures"]
    assert result["financial_context"]["debt"] is None
    assert result["financial_context"]["fcf_ttm"] == -20.0


def test_recovery_prior_sign_is_not_backfilled_from_current_minus_change():
    data = payloads()
    change(data, "operating_profit")["from_value"] = None
    result = evaluate(data)
    assert result["routes"]["operating_recovery"]["state"] == "matched"
    assert result["routes"]["operating_recovery"]["profitability"] == {
        "current_quarter": "positive",
        "comparison_prior_quarter": None,
    }


def test_worsening_and_basis_gaps_do_not_get_a_recovery_profile_or_forecast_rescue():
    data = payloads()
    change(data, "operating_profit")["difference"] = -1.0
    current(data, "balance")["accounting_standard"] = None
    data["company_financials"]["financial"].update(
        forecast_profit_yen=99999.0, forecast_full_year_loss_flag=False
    )
    data["technical_analysis"] = {"arbitrary_score": 999999.0}
    result = evaluate(data)
    assert result["state"] == "not_selected"
    route = result["routes"]["operating_recovery"]
    assert route["profitability"] is None
    assert route["failures"] == ["quarter_operating_profit_increase"]
    assert route["missing_by_kind"]["unresolved_comparability"] == ["balance_quarter_basis"]
