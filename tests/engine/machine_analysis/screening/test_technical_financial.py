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


def test_each_stock_uses_its_own_financial_observations_even_without_technical_data(
    tmp_path, monkeypatch
):
    fixed = _seed(tmp_path / "runs")
    before = fixed.model_dump_json()
    meta = GenerationMetadata.model_validate(fixed.model_dump(exclude={"models"}))
    real = observations._financial_growth_with_path_inputs
    calls = []

    def read(ticker, **kwargs):
        calls.append(ticker)
        assert {m.model_id for m in kwargs["models"]} <= {"company_growth", "company_financials"}
        return real(ticker, **kwargs)

    monkeypatch.setattr(observations, "_financial_growth_with_path_inputs", read)
    universe = tuple(t for t, p in meta.subject_plan.items() if p.scope == "stock")
    by = {
        t: observations.project_discovery_row(
            t, models=tuple(m for m in fixed.models if m.subject == t), metadata=meta
        )
        for t in universe
    }
    saved = observations.evaluate_discovery_rows(meta, tickers=universe, rows=by)
    assert calls == list(universe)
    assert saved.result.tickers == saved.universe_tickers == tuple(sorted(universe))
    assert by["2345"]["financial_state"] == "not_selected"
    assert by["3456"]["financial_state"] == "input_unresolved"
    assert by["5678"]["technical_state"] == "unavailable"
    assert by["5678"]["financial_state"] == "research_candidate"
    assert saved.result.rows == tuple(by[t] for t in sorted(universe))
    assert fixed.model_dump_json() == before
