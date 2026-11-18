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
from tests.helpers.technical_discovery import _metadata, _models

from baibai_engine.machine_analysis.models.company_financials.earnings import (
    EarningsPrice,
    ProfitComponent,
)
from baibai_engine.machine_analysis.screening.policies.discovery import (
    project_discovery_row,
    read_discovery_observations,
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
    result = project_discovery_row("1234", models=models(data), metadata=_metadata())
    assert data == before
    return result


def observations(data):
    return read_discovery_observations("1234", models=models(data), metadata=_metadata())


@pytest.mark.parametrize("technical", [False, True])
@pytest.mark.parametrize(
    ("expansion", "path"), [(False, "earning_power"), (True, "business_expansion")]
)
def test_financial_coordinates_do_not_require_a_technical_match_or_growth_route(
    technical, expansion, path
):
    row = project_unchanged(case(expansion=expansion, technical=technical))
    assert row["financial_state"] == "not_selected"
    assert row["matched_paths"] == {"financial": [path], "growth": []}
    assert row["screening_lane"] == "alternative_financial_research"
    assert {c["path_id"] for c in row["coordinates"]} == {path}
    assert all(
        c["price_value"] is not None and c["business_value"] is not None for c in row["coordinates"]
    )


@pytest.mark.parametrize(
    "reason", ["missing_capital_basis", "indeterminate_treasury_after_later_issued_observation"]
)
@pytest.mark.parametrize("expansion", [False, True])
@pytest.mark.parametrize("technical", [False, True])
def test_supported_share_gap_requires_review_not_growth_or_clearance(reason, expansion, technical):
    data = case(reason=reason, expansion=expansion, technical=technical)
    row = project_unchanged(data)
    assert row["financial_state"] == "not_selected"
    assert row["matched_paths"] == {"financial": [], "growth": []}
    assert row["coordinates"] == []
    assert row["screening_lane"] == "input_review"
    assert f"financial:{reason}" in row["missing"]
    assert data["company_financials"]["earnings_price"]["shares_ex_treasury"] is None


@pytest.mark.parametrize(
    "problem",
    [
        "missing_business_operand",
        "different_income_basis",
        "unknown_reason",
        "known_zero_shares",
        "old_price",
        "no_positive_profit",
        "unknown_sector",
        "missing_profit",
    ],
)
def test_no_supported_financial_hypothesis_is_not_a_share_only_gap(problem):
    data = case(
        reason=None if problem in {"unknown_sector", "missing_profit"} else "missing_capital_basis"
    )
    earnings = data["company_financials"]["earnings_price"]
    if problem == "missing_business_operand":
        current(data, "annual_ttm")["metrics"]["operating_margin"].update(
            value=None, reason="metric_not_observed"
        )
    elif problem == "different_income_basis":
        earnings["statement_basis"] = "nonconsolidated"
    elif problem == "unknown_reason":
        earnings["unavailable_reason"] = "synthetic_unknown_failure"
    elif problem == "known_zero_shares":
        earnings["shares_ex_treasury"] = 0.0
    elif problem == "old_price":
        earnings["price_date"] = "2026-11-12"
    elif problem == "no_positive_profit":
        earnings["profit_ttm_yen"] = earnings["profit_components"][0]["amount_yen"] = -50.0
    elif problem == "unknown_sector":
        data["market_context"]["master"]["sector_33"] = None
    else:
        earnings.update(
            earnings_yield=None,
            profit_ttm_yen=None,
            profit_components=[],
            unavailable_reason="unavailable_annual_profit",
        )
        assert earnings["shares_ex_treasury"] == 100.0
    row = project_unchanged(data)
    assert row["financial_state"] == "not_selected"
    assert row["screening_lane"] == "no_current_hypothesis"
    assert row["coordinates"] == []
    assert not any(k.startswith("financial:") for k in row["missing"])
    if problem == "no_positive_profit":
        assert row["technical_state"] == "matched"


def test_known_technical_nonmatch_does_not_hide_share_review():
    from tests.helpers.technical_financial import _no_match

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
    assert row["technical_state"] == "not_matched"
    assert row["financial_state"] == "not_selected"
    assert row["screening_lane"] == "input_review"


def test_review_references_selected_path_and_same_model_without_changing_coordinates():
    data = case(reason="missing_capital_basis")
    _, _, coordinates, reasons = observations(data)
    row = project_unchanged(data)
    assert row["missing"] == ["financial:missing_capital_basis"]
    assert row["coordinates"] == []
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
    assert not matching.eligible
    assert matching.price_value is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("price_yen", None), ("price_yen", 0), ("shares_ex_treasury", -1), ("market_cap_yen", 100)],
)
def test_reason_does_not_override_inconsistent_or_nonpositive_pricing(field, value):
    data = case(reason="missing_capital_basis")
    data["company_financials"]["earnings_price"][field] = value
    assert project_unchanged(data)["screening_lane"] == "no_current_hypothesis"


def test_future_profit_source_is_not_known_earning_power():
    data = case(reason="missing_capital_basis")
    data["company_financials"]["earnings_price"]["profit_components"][0]["disclosed_at"] = (
        "2026-11-15"
    )
    assert project_unchanged(data)["screening_lane"] == "no_current_hypothesis"


@pytest.mark.parametrize("problem", ["missing_rate", "comparison_basis", "missing_sales"])
def test_sales_review_does_not_invent_rate_or_rescue_incomparable_operands(problem):
    data = case(expansion=True, reason="missing_capital_basis")
    if problem == "missing_rate":
        change(data, "sales")["rate"] = None
    elif problem == "comparison_basis":
        change(data, "sales").update(difference=None, rate=None, reason="comparison_basis_mismatch")
    else:
        current(data, "annual_ttm")["metrics"]["sales"].update(value=None, reason="not_observed")
    assert project_unchanged(data)["screening_lane"] == "no_current_hypothesis"


@pytest.mark.parametrize("newer_loss", [False, True])
def test_full_fy_review_uses_selected_observation_and_does_not_hide_newer_loss(newer_loss):
    from tests.engine.machine_analysis.screening.test_full_fy_fallback import documents, generation

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
    _, _, _, reasons = read_discovery_observations("1001", models=rows, metadata=fixed)
    earning = [item for item in reasons if item["path_id"] == "earning_power"]
    assert bool(earning) is not newer_loss
    if earning:
        assert earning[0]["variant"] == "full_fy"
    assert fixed.model_dump_json() == before


def test_available_ttm_does_not_select_more_attractive_fy_for_review():
    from tests.engine.machine_analysis.screening.test_full_fy_fallback import documents, generation

    fixed = generation(documents(prior_quarter=True))
    cf = next(model.payload for model in fixed.models if model.model_id == "company_financials")
    cf["earnings_price"].update(
        shares_ex_treasury=None,
        market_cap_yen=None,
        earnings_yield=None,
        unavailable_reason="missing_capital_basis",
    )
    _, _, _, reasons = read_discovery_observations("1001", models=fixed.models, metadata=fixed)
    assert [item["variant"] for item in reasons if item["path_id"] == "earning_power"] == [
        "annual_ttm"
    ]


def test_negative_selected_ttm_does_not_borrow_positive_fy_for_earning_review():
    from tests.engine.machine_analysis.screening.test_full_fy_fallback import documents, generation

    fixed = generation(documents(prior_quarter=True))
    cf = next(model.payload for model in fixed.models if model.model_id == "company_financials")
    cf["earnings_price"].update(
        shares_ex_treasury=None,
        market_cap_yen=None,
        earnings_yield=None,
        profit_ttm_yen=-1.0,
        unavailable_reason="missing_capital_basis",
    )
    _, _, _, reasons = read_discovery_observations("1001", models=fixed.models, metadata=fixed)
    assert not any(item["path_id"] == "earning_power" for item in reasons)
