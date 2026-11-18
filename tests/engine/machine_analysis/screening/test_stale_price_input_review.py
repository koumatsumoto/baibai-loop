"""古い価格で事業仮説を消さず、価格・財務合格へも置き換えない。"""

from copy import deepcopy

import pytest
from tests.engine.machine_analysis.screening.test_financial_input_review import (
    case,
    current,
    observations,
    project_unchanged,
)


def stale_case(*, expansion=False, technical=False):
    data = case(reason="stale_price", expansion=expansion, technical=technical)
    data["company_financials"]["earnings_price"].update(
        price_date="2026-11-12", shares_ex_treasury=100.0, market_cap_yen=10000.0
    )
    return data


@pytest.mark.parametrize("technical", [False, True])
@pytest.mark.parametrize(
    ("expansion", "path"), [(False, "earning_power"), (True, "business_expansion")]
)
def test_stale_price_keeps_selected_nonprice_support_without_financial_clearance(
    expansion, path, technical
):
    data = stale_case(expansion=expansion, technical=technical)
    row = project_unchanged(data)
    baseline = project_unchanged(case(expansion=expansion, technical=technical))
    _, _, coordinates, reviews = observations(data)
    assert row["financial_state"] == baseline["financial_state"] == "not_selected"
    assert row["technical_state"] == baseline["technical_state"]
    assert row["screening_lane"] == "input_review"
    assert row["matched_paths"]["financial"] == row["coordinates"] == []
    assert "financial:stale_price" in row["missing"]
    assert not any(p.eligible for p in coordinates.paths)
    assert all(p.price_value is None for p in coordinates.paths)
    (reason,) = reviews
    assert reason["path_id"] == path
    assert reason["reason"] == "stale_price"
    assert reason["source_field"] == "earnings_price.price_date"
    assert reason["price_date"] == "2026-11-12"
    assert reason["expected_price_date"] == "2026-11-13"
    assert reason["interpretation"] == "stale_price_review_not_current_price_or_financial_clearance"
    selected = next(
        p for p in coordinates.paths if (p.path_id, p.variant) == (path, reason["variant"])
    )
    assert selected.business_value is not None
    assert selected.business_value > 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_yen", None),
        ("price_yen", 0),
        ("price_yen", -1),
        ("price_date", None),
        ("price_date", "2026-11-13"),
        ("price_date", "2026-11-14"),
        ("shares_ex_treasury", None),
        ("shares_ex_treasury", 0),
        ("market_cap_yen", None),
        ("unavailable_reason", "unavailable_annual_profit"),
        ("unavailable_reason", "missing_capital_basis"),
    ],
)
def test_stale_reason_does_not_rescue_other_missing_or_invalid_price_inputs(field, value):
    data = stale_case()
    data["company_financials"]["earnings_price"][field] = value
    row = project_unchanged(data)
    assert row["screening_lane"] == "no_current_hypothesis"
    assert "financial:stale_price" not in row["missing"]


@pytest.mark.parametrize("expected", [None, "invalid-date", "2026-11-31", "2026-11-15"])
def test_stale_price_needs_a_known_expected_date_no_later_than_as_of(expected):
    data = stale_case()
    data["company_financials"]["expected_price_date"] = expected
    row = project_unchanged(data)
    assert row["screening_lane"] == "no_current_hypothesis"
    assert "financial:stale_price" not in row["missing"]


@pytest.mark.parametrize(
    "problem", ["missing_margin", "basis", "nonpositive_profit", "future_profit"]
)
def test_stale_price_does_not_supply_missing_earning_power(problem):
    data = stale_case()
    earnings = data["company_financials"]["earnings_price"]
    if problem == "missing_margin":
        current(data, "annual_ttm")["metrics"]["operating_margin"].update(
            value=None, reason="not_observed"
        )
    elif problem == "basis":
        earnings["statement_basis"] = "nonconsolidated"
    elif problem == "nonpositive_profit":
        earnings["profit_ttm_yen"] = 0.0
        earnings["profit_components"][0]["amount_yen"] = 0.0
    else:
        earnings["profit_components"][0]["disclosed_at"] = "2026-11-15"
    assert project_unchanged(data)["screening_lane"] == "no_current_hypothesis"


def test_expansion_review_retains_loss_deterioration_and_negative_net_assets():
    data = stale_case(expansion=True)
    current(data, "balance")["metrics"]["net_assets"]["value"] = -22.0
    without_price_review = deepcopy(data)
    without_price_review["company_financials"]["earnings_price"]["unavailable_reason"] = "other"
    _, gate, _, reviews = observations(data)
    assert gate == observations(without_price_review)[1]
    assert gate["checks"]["positive_net_assets"]["value"] == -22.0
    assert gate["checks"]["quarter_operating_profit_increase"]["value"] == -10.0
    assert gate["routes"]["operating_recovery"]["state"] == "not_matched"
    assert project_unchanged(data)["screening_lane"] == "input_review"
    assert [p["path_id"] for p in reviews] == ["business_expansion"]
