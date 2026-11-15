"""株数基準の未確定を、Gの既知非充足や価格合格へ置換しない。"""

from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_financial_growth import (
    change,
    current,
    models,
    payloads,
)
from tests.tools.test_technical_discovery import _metadata, _models

from baibai_engine.machine_analysis.models.company_financials.earnings import (
    EarningsPrice,
    ProfitComponent,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    project_technical_financial_row,
)


def case(*, expansion=False, reason=None, technical=True):
    """A financial hypothesis, not a named company or forecast of its value."""
    data = payloads()
    for metric in ("sales", "operating_profit", "operating_margin"):
        item = change(data, metric)
        item.update(from_value=item["to_value"], difference=0.0, rate=0.0)
    profit = 50.0
    if expansion:
        profit = -50.0
        current(data, "annual_ttm")["metrics"]["operating_profit"]["value"] = -100.0
        current(data, "annual_ttm")["metrics"]["operating_margin"]["value"] = -0.1
        current(data, "quarter")["metrics"]["operating_profit"]["value"] = -20.0
        current(data, "quarter")["metrics"]["operating_margin"]["value"] = -0.02
        change(data, "sales").update(
            from_value=900.0, to_value=1000.0, difference=100.0, rate=1 / 9
        )
        change(data, "operating_profit").update(
            from_value=-10.0,
            to_value=-20.0,
            difference=-10.0,
            rate=None,
            reason="nonpositive_base",
        )
        change(data, "operating_margin").update(
            from_value=-0.01, to_value=-0.02, difference=-0.01, rate=None
        )
    unavailable = reason is not None
    data["company_financials"].update(
        expected_price_date="2026-11-13",
        earnings_price=TypeAdapter(EarningsPrice).dump_python(
            EarningsPrice(
                as_of=date(2026, 11, 14),
                price_date=date(2026, 11, 13),
                price_yen=100.0,
                shares_ex_treasury=None if unavailable else 100.0,
                market_cap_yen=None if unavailable else 10000.0,
                profit_ttm_yen=profit,
                statement_basis="consolidated",
                accounting_standard="JP",
                profit_components=(
                    ProfitComponent(
                        coefficient=1,
                        amount_yen=profit,
                        period_start=date(2025, 10, 1),
                        period_end=date(2026, 9, 30),
                        disclosed_at=date(2026, 10, 30),
                        disclosed_time="15:00:00",
                        disclosure_number="synthetic-current-profit",
                        document_type="FinancialStatements_Consolidated_JP",
                    ),
                ),
                earnings_yield=None if unavailable else profit / 10000.0,
                unavailable_reason=reason,
            ),
            mode="json",
        ),
    )
    # Disable capital_productivity so it cannot mask the tested path.
    data["company_financials"]["financial"].update(
        market_cap=None if unavailable else 10000.0, pbr=None
    )
    for model in _models():
        if model.model_id != "technical_analysis" or technical:
            data[model.model_id] = deepcopy(model.payload)
    data["market_context"]["master"].update(sector_33="情報・通信業", name="合成交差例")
    return data


def project_unchanged(data):
    before = deepcopy(data)
    result = project_technical_financial_row("1234", models=models(data), metadata=_metadata())
    assert data == before
    return result


@pytest.mark.parametrize(
    ("expansion", "path"), [(False, "earning_power"), (True, "business_expansion")]
)
def test_complete_control_is_an_alternative_not_growth(expansion, path):
    row = project_unchanged(case(expansion=expansion))
    assert row.financial_gate["state"] == "not_selected"
    assert row.financial_paths["matched_routes"] == [path]
    assert row.screening_lane == "alternative_financial_research"


@pytest.mark.parametrize(
    "reason",
    ["missing_capital_basis", "indeterminate_treasury_after_later_issued_observation"],
)
@pytest.mark.parametrize("expansion", [False, True])
@pytest.mark.parametrize("technical", [False, True])
def test_supported_price_input_gap_is_review_not_growth_or_eligibility(
    reason, expansion, technical
):
    baseline = project_unchanged(case(expansion=expansion, technical=technical))
    data = case(reason=reason, expansion=expansion, technical=technical)
    row = project_unchanged(data)
    assert row.financial_gate == baseline.financial_gate
    assert row.financial_gate["state"] == "not_selected"
    assert row.financial_paths["matched_routes"] == []
    assert not any(path["eligible"] for path in row.financial_paths["paths"])
    assert all(path["price_value"] is None for path in row.financial_paths["paths"])
    assert data["company_financials"]["earnings_price"]["shares_ex_treasury"] is None
    assert row.screening_lane == "input_review"
    assert "action" not in row.financial_gate


def test_positive_expansion_does_not_erase_observed_loss_deterioration():
    row = project_unchanged(case(expansion=True, reason="missing_capital_basis"))
    assert row.financial_gate["checks"]["quarter_operating_profit_increase"]["state"] == "not_met"
    assert row.financial_gate["checks"]["quarter_operating_profit_increase"]["value"] == -10.0
    assert row.financial_gate["routes"]["operating_recovery"]["state"] == "not_matched"
    assert row.screening_lane == "input_review"


def test_missing_business_operand_is_not_a_price_only_gap():
    data = case(reason="missing_capital_basis")
    current(data, "annual_ttm")["metrics"]["operating_margin"].update(
        value=None, reason="metric_not_observed"
    )
    row = project_unchanged(data)
    assert row.financial_gate["state"] == "not_selected"
    assert row.screening_lane == "no_current_hypothesis"


def test_mismatched_income_basis_is_not_a_price_only_gap():
    data = case(reason="missing_capital_basis")
    data["company_financials"]["earnings_price"]["statement_basis"] = "nonconsolidated"
    row = project_unchanged(data)
    assert row.financial_gate["state"] == "not_selected"
    assert row.screening_lane == "no_current_hypothesis"


def test_unknown_reason_is_not_treated_as_known_missing_shares():
    row = project_unchanged(case(reason="synthetic_unknown_failure"))
    assert row.financial_gate["state"] == "not_selected"
    assert row.screening_lane == "no_current_hypothesis"


def test_known_nonpositive_share_count_is_not_missing_share_count():
    data = case(reason="missing_capital_basis")
    data["company_financials"]["earnings_price"]["shares_ex_treasury"] = 0.0
    row = project_unchanged(data)
    assert row.financial_gate["state"] == "not_selected"
    assert row.screening_lane == "no_current_hypothesis"


def test_current_price_mismatch_is_not_a_share_only_gap():
    data = case(reason="missing_capital_basis")
    data["company_financials"]["earnings_price"]["price_date"] = "2026-11-12"
    row = project_unchanged(data)
    assert row.financial_gate["state"] == "not_selected"
    assert row.screening_lane == "no_current_hypothesis"


def test_no_positive_business_hypothesis_is_not_promoted_by_technical_match():
    data = case(reason="missing_capital_basis")
    data["company_financials"]["earnings_price"]["profit_ttm_yen"] = -50.0
    data["company_financials"]["earnings_price"]["profit_components"][0]["amount_yen"] = -50.0
    row = project_unchanged(data)
    assert row.technical["state"] == "matched"
    assert row.financial_gate["state"] == "not_selected"
    assert row.screening_lane == "no_current_hypothesis"


def test_unsupported_sector_only_gap_is_not_silently_part_of_first_price_fix():
    data = case()
    data["market_context"]["master"]["sector_33"] = None
    row = project_unchanged(data)
    assert row.financial_gate["state"] == "not_selected"
    assert row.screening_lane == "no_current_hypothesis"


def test_price_coordinate_null_from_profit_gap_is_not_a_share_gap():
    data = case()
    data["company_financials"]["earnings_price"].update(
        earnings_yield=None,
        profit_ttm_yen=None,
        profit_components=[],
        unavailable_reason="unavailable_annual_profit",
    )
    row = project_unchanged(data)
    assert data["company_financials"]["earnings_price"]["shares_ex_treasury"] == 100.0
    assert row.financial_gate["state"] == "not_selected"
    assert row.financial_paths["matched_routes"] == []
    assert row.screening_lane == "no_current_hypothesis"


def test_expansion_input_review_keeps_known_negative_net_assets():
    data = case(expansion=True, reason="missing_capital_basis", technical=False)
    current(data, "balance")["metrics"]["net_assets"]["value"] = -22.0
    row = project_unchanged(data)
    assert row.financial_gate["state"] == "not_selected"
    assert row.financial_gate["checks"]["positive_net_assets"] == {
        "state": "not_met",
        "value": -22.0,
        "reason": None,
    }
    assert "positive_net_assets" in row.financial_gate["routes"]["operating_recovery"]["failures"]
    assert row.financial_paths["matched_routes"] == []
    assert row.screening_lane == "input_review"


def test_known_technical_nonmatch_does_not_hide_share_review():
    from tests.tools.test_technical_financial_candidates import _no_match

    data = case(reason="missing_capital_basis")
    from tests.engine.machine_analysis.screening.test_technical_strategy import _strategy_models

    technical = _strategy_models()
    _no_match(technical)
    technical[1].payload["relative"]["market_conditioned_60d"]["benchmarks"]["1321"]["down"][
        "own_mean_return"
    ] = -0.1
    for model in technical:
        if model.model_id == "technical_analysis":
            data[model.model_id] = model.payload
        else:
            data[model.model_id].update({k: v for k, v in model.payload.items() if k != "master"})
    row = project_unchanged(data)
    assert row.technical["state"] == "not_matched"
    assert row.financial_gate["state"] == "not_selected"
    assert row.screening_lane == "input_review"


def test_review_references_selected_path_and_same_model_without_changing_coordinates():
    from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
        project_company,
        project_company_with_input_reviews,
    )

    data = case(reason="missing_capital_basis")
    coordinates, reasons = project_company_with_input_reviews(
        "1234", models=models(data), metadata=_metadata()
    )
    assert coordinates == project_company("1234", models=models(data), metadata=_metadata())
    row = project_unchanged(data)
    assert reasons == tuple(row.financial_paths["input_review_reasons"])
    assert reasons == (
        {
            "path_id": "earning_power",
            "variant": "annual_ttm",
            "reason": "missing_capital_basis",
            "source_field": "earnings_price.shares_ex_treasury",
            "source_model_id": "company_financials",
            "source_model_revision": "test-v1",
            "generation_id": _metadata().generation_id,
            "subject": "1234",
            "interpretation": "share_basis_review_not_price_or_financial_clearance",
        },
    )
    matching = next(
        path
        for path in coordinates.paths
        if (path.path_id, path.variant) == (reasons[0]["path_id"], reasons[0]["variant"])
    )
    assert matching.evidence["selected_variant"] == reasons[0]["variant"]
    assert not matching.eligible
    assert matching.price_value is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("price_yen", None), ("price_yen", 0), ("shares_ex_treasury", -1), ("market_cap_yen", 100)],
)
def test_reason_does_not_override_inconsistent_or_nonpositive_pricing(field, value):
    data = case(reason="missing_capital_basis")
    data["company_financials"]["earnings_price"][field] = value
    assert project_unchanged(data).screening_lane == "no_current_hypothesis"


def test_future_profit_source_is_not_known_earning_power():
    data = case(reason="missing_capital_basis")
    data["company_financials"]["earnings_price"]["profit_components"][0]["disclosed_at"] = (
        "2026-11-15"
    )
    assert project_unchanged(data).screening_lane == "no_current_hypothesis"


@pytest.mark.parametrize("problem", ["missing_rate", "comparison_basis", "missing_sales"])
def test_sales_review_does_not_invent_rate_or_rescue_incomparable_operands(problem):
    data = case(expansion=True, reason="missing_capital_basis")
    if problem == "missing_rate":
        change(data, "sales")["rate"] = None
    elif problem == "comparison_basis":
        change(data, "sales").update(difference=None, rate=None, reason="comparison_basis_mismatch")
    else:
        current(data, "annual_ttm")["metrics"]["sales"].update(value=None, reason="not_observed")
    assert project_unchanged(data).screening_lane == "no_current_hypothesis"


@pytest.mark.parametrize("newer_loss", [False, True])
def test_full_fy_review_uses_selected_observation_and_does_not_hide_newer_loss(newer_loss):
    from tests.engine.machine_analysis.screening.test_full_fy_fallback import documents, generation

    from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
        project_company_with_input_reviews,
    )

    docs = documents()
    if newer_loss:
        docs[-1] = replace(docs[-1], operating_profit=-1.0)
    fixed = generation(docs)
    before = fixed.model_dump_json()
    rows = deepcopy(fixed.models)
    cf = next(model.payload for model in rows if model.model_id == "company_financials")
    cf["earnings_price"].update(
        shares_ex_treasury=None,
        market_cap_yen=None,
        earnings_yield=None,
        unavailable_reason="missing_capital_basis",
    )
    _, reasons = project_company_with_input_reviews("1001", models=rows, metadata=fixed)
    earning = [item for item in reasons if item["path_id"] == "earning_power"]
    assert bool(earning) is not newer_loss
    if earning:
        assert earning[0]["variant"] == "full_fy"
    assert fixed.model_dump_json() == before


def test_available_ttm_does_not_select_more_attractive_fy_for_review():
    from tests.engine.machine_analysis.screening.test_full_fy_fallback import documents, generation

    from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
        project_company_with_input_reviews,
    )

    fixed = generation(documents(prior_quarter=True))
    cf = next(model.payload for model in fixed.models if model.model_id == "company_financials")
    cf["earnings_price"].update(
        shares_ex_treasury=None,
        market_cap_yen=None,
        earnings_yield=None,
        unavailable_reason="missing_capital_basis",
    )
    _, reasons = project_company_with_input_reviews("1001", models=fixed.models, metadata=fixed)
    assert [item["variant"] for item in reasons if item["path_id"] == "earning_power"] == [
        "annual_ttm"
    ]


def test_negative_selected_ttm_does_not_borrow_positive_fy_for_earning_review():
    from tests.engine.machine_analysis.screening.test_full_fy_fallback import documents, generation

    from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
        project_company_with_input_reviews,
    )

    fixed = generation(documents(prior_quarter=True))
    cf = next(model.payload for model in fixed.models if model.model_id == "company_financials")
    cf["earnings_price"].update(
        shares_ex_treasury=None,
        market_cap_yen=None,
        earnings_yield=None,
        profit_ttm_yen=-1.0,
        unavailable_reason="missing_capital_basis",
    )
    _, reasons = project_company_with_input_reviews("1001", models=fixed.models, metadata=fixed)
    assert not any(item["path_id"] == "earning_power" for item in reasons)
