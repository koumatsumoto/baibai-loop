"""G未確定でも既存の非価格支持を渡し、赤字・比較不能を消さない。"""

from copy import deepcopy

import pytest
from tests.engine.machine_analysis.screening.test_financial_input_review import (
    case,
    change,
    current,
    observations,
    project_unchanged,
)
from tests.engine.machine_analysis.screening.test_stale_price_input_review import stale_case

from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    financial_screening_lane,
)


def unresolved_case(*, expansion=False, technical=False, stale=False):
    data = (
        stale_case(expansion=expansion, technical=technical)
        if stale
        else case(expansion=expansion, technical=technical, reason="missing_capital_basis")
    )
    for metric in ("sales", "operating_profit", "operating_margin"):
        change(data, metric).update(
            from_value=None,
            difference=None,
            rate=None,
            reason="comparable_prior_period_not_observed",
        )
    if expansion:
        data["company_growth"]["changes"].append(
            {
                **change(data, "sales"),
                "measurement": "annual_ttm",
                "from_start": "2024-10-01",
                "from_end": "2025-09-30",
                "to_start": "2025-10-01",
                "to_end": "2026-09-30",
                "from_value": 900.0,
                "to_value": 1000.0,
                "difference": 100.0,
                "rate": 1 / 9,
                "reason": None,
            }
        )
    return data


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("technical", [False, True])
@pytest.mark.parametrize("expansion", [False, True])
def test_unresolved_preserves_supported_input_reason_and_all_other_states(
    stale, technical, expansion
):
    data = unresolved_case(stale=stale, technical=technical, expansion=expansion)
    no_reason = deepcopy(data)
    no_reason["company_financials"]["earnings_price"]["unavailable_reason"] = "other"
    row, control = project_unchanged(data), project_unchanged(no_reason)
    _, gate, coordinates, _ = observations(data)
    assert row["financial_state"] == control["financial_state"] == "input_unresolved"
    assert row["technical_state"] == control["technical_state"]
    assert row["screening_lane"] == "input_review"
    assert row["matched_paths"]["financial"] == row["coordinates"] == []
    assert not any(p.eligible for p in coordinates.paths)
    assert all(p.price_value is None for p in coordinates.paths)
    reason = "stale_price" if stale else "missing_capital_basis"
    assert f"financial:{reason}" in row["missing"]
    assert gate.state == "input_unresolved"
    assert not gate.matched_paths
    assert "quarter_sales_increase" in gate.missing
    assert {"growth:" + name for name in gate.missing} == {
        name for name in row["missing"] if name.startswith("growth:")
    }
    if expansion:
        assert "positive_quarter_operating_profit" not in gate.missing
        assert "positive_annual_operating_profit" not in gate.missing


@pytest.mark.parametrize("problem", ["no_support", "unknown_price_reason", "missing_model"])
def test_unresolved_alone_is_not_a_reason_to_enter_review(problem):
    data = unresolved_case()
    if problem == "no_support":
        current(data, "annual_ttm")["metrics"]["operating_margin"].update(
            value=None, reason="not_observed"
        )
    elif problem == "unknown_price_reason":
        data["company_financials"]["earnings_price"]["unavailable_reason"] = "other"
    else:
        del data["company_financials"]
    row = project_unchanged(data)
    assert row["financial_state"] in {"input_unresolved", "not_evaluated"}
    assert row["screening_lane"] == "no_current_hypothesis"
    assert not any(k.startswith("financial:") for k in row["missing"])


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("research_candidate", "growth_research"),
        ("not_evaluated", "no_current_hypothesis"),
        ("input_unresolved", "input_review"),
        ("not_selected", "input_review"),
    ],
)
def test_input_reason_does_not_override_growth_or_unevaluated_state(state, expected):
    assert (
        financial_screening_lane(
            growth_state=state,
            technical_match=False,
            financial_path_match=False,
            financial_input_review=True,
        )
        == expected
    )
