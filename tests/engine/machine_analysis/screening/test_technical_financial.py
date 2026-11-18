"""新しい候補政策の接続・全分母・表示容量。投資優位はtestしない。"""

import pytest
from pydantic import ValidationError
from tests.helpers.technical_financial import _seed

from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.machine_analysis.screening.policies import discovery as observations
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialRow,
    financial_screening_lane,
)


def row(ticker, routes=("a",), state="research_candidate"):
    return TechnicalFinancialRow(
        ticker,
        ticker,
        "業種",
        {
            "state": "matched" if routes else "not_matched",
            "matched_routes": list(routes),
            "routes": {},
            "price_context": {"ready": True},
        },
        {"state": state, "matched_routes": [], "routes": {}, "checks": {}},
        {"matched_routes": [], "paths": []},
        financial_screening_lane(
            growth_state=state, technical_match=bool(routes), financial_path_match=False
        ),
    )


@pytest.mark.parametrize("size", [0, -1, 37, True])
def test_page_size_is_bounded_not_a_research_budget(size):
    with pytest.raises(ValidationError):
        TechnicalFinancialPolicy(page_size=size)


@pytest.mark.parametrize(
    ("growth", "technical", "financial", "expected"),
    [
        ("research_candidate", False, False, "growth_research"),
        ("research_candidate", True, True, "growth_research"),
        ("input_unresolved", False, True, "input_review"),
        ("input_unresolved", True, False, "input_review"),
        ("input_unresolved", False, False, "no_current_hypothesis"),
        ("not_selected", True, True, "alternative_financial_research"),
        ("not_selected", False, True, "alternative_financial_research"),
        ("not_selected", True, False, "no_current_hypothesis"),
    ],
)
def test_financial_lanes_do_not_overwrite_growth_failure(growth, technical, financial, expected):
    assert (
        financial_screening_lane(
            growth_state=growth, technical_match=technical, financial_path_match=financial
        )
        == expected
    )


def test_each_stock_uses_its_own_financial_observations_even_without_technical_data(tmp_path):
    fixed = _seed(tmp_path / "runs")
    before = fixed.model_dump_json()
    meta = GenerationMetadata.model_validate(fixed.model_dump(exclude={"models"}))
    universe = tuple(t for t, p in meta.subject_plan.items() if p.scope == "stock")
    by = {
        t: observations.project_discovery_row(
            t, models=tuple(m for m in fixed.models if m.subject == t), metadata=meta
        )
        for t in universe
    }
    saved = observations.evaluate_discovery_rows(meta, tickers=universe, rows=by)
    assert saved.result.tickers == saved.universe_tickers == tuple(sorted(universe))
    assert by["2345"]["financial_state"] == "not_selected"
    assert by["3456"]["financial_state"] == "input_unresolved"
    assert by["5678"]["technical_state"] == "unavailable"
    assert by["5678"]["financial_state"] == "research_candidate"
    assert saved.result.rows == tuple(by[t] for t in sorted(universe))
    assert fixed.model_dump_json() == before


@pytest.mark.parametrize(
    ("fault", "used_by_discovery"),
    [
        ("unrelated_period", False),
        ("unrelated_share_change", False),
        ("current_period", True),
        ("comparison_prior", True),
        ("coverage", True),
    ],
)
def test_discovery_is_not_audit_of_an_entire_company_history(fault, used_by_discovery):
    from copy import deepcopy

    from tests.engine.machine_analysis.screening.test_financial_growth import (
        change,
        metadata,
        models,
        payloads,
    )

    from baibai_engine.machine_analysis.models.company_growth.models import CompanyGrowth

    data = payloads()
    raw = data["company_growth"]
    prior = deepcopy(raw["periods"][-1])
    prior.update(
        fiscal_period="3Q",
        fiscal_year_end="2025-12-31",
        period_start="2025-01-01",
        period_end="2025-09-30",
    )
    prior_quarter = prior["observations"][0]
    prior_quarter.update(period_start="2025-07-01", period_end="2025-09-30")
    prior["observations"] = [prior_quarter]
    raw["periods"].insert(0, prior)
    row = observations.project_discovery_row("1234", models=models(data), metadata=metadata())
    if fault == "unrelated_period":
        raw["periods"].insert(
            0,
            {
                "fiscal_period": "1Q",
                "fiscal_year_end": "2016-12-31",
                "period_start": "2016-01-01",
                "period_end": "not-a-date",
                "observations": [],
            },
        )
    elif fault == "unrelated_share_change":
        raw["changes"].append(
            {**change(data, "sales"), "metric": "average_shares", "difference": "not-a-number"}
        )
    elif fault == "current_period":
        raw["periods"][-1]["period_end"] = "not-a-date"
    elif fault == "comparison_prior":
        prior_quarter["metrics"]["sales"]["value"] = "not-a-number"
    else:
        raw["coverage"]["observed_period_ends"] = ["not-a-date"]

    with pytest.raises(ValidationError):
        CompanyGrowth.model_validate(raw)
    if used_by_discovery:
        with pytest.raises(ValidationError):
            observations.project_discovery_row("1234", models=models(data), metadata=metadata())
    else:
        assert (
            observations.project_discovery_row("1234", models=models(data), metadata=metadata())
            == row
        )
