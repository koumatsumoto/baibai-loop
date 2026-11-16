"""営業回復と親会社利益を同一視せず、同じ採用観測のbasisと不足を渡す。"""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_financial_growth import (
    add_prior_fy,
    current,
    metadata,
    payloads,
)
from tests.engine.machine_analysis.screening.test_growth_period_context import project
from tests.engine.machine_analysis.screening.test_technical_financial import row

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    evaluate_technical_financial_rows,
)


def test_same_observation_income_basis_units_and_documents_reach_first_card():
    data = payloads()
    quarter = current(data, "quarter")
    quarter["metrics"]["operating_profit"]["value"] = -10
    quarter["metrics"]["parent_profit"] = {
        "value": 20,
        "reason": None,
        "documents": deepcopy(quarter["metrics"]["operating_profit"]["documents"]),
    }
    annual = current(data, "annual_ttm")
    annual["metrics"]["parent_profit"] = {"value": 150, "reason": None, "documents": []}
    before = deepcopy(data)
    actual = project(data)
    context = actual.financial_gate["income_attribution_context"]
    assert context["metric_units"] == {"operating_profit": "JPY", "parent_profit": "JPY"}
    assert context["annual_variant"] == "annual_ttm"
    assert context["quarter"] == {
        key: value for key, value in quarter.items() if key not in ("share_basis", "metrics")
    } | {"metrics": {key: quarter["metrics"][key] for key in ("operating_profit", "parent_profit")}}
    assert context["annual"]["metrics"]["parent_profit"]["value"] == 150
    assert context["annual"]["period_start"] == "2025-10-01"
    saved = evaluate_technical_financial_rows(
        metadata(), tickers=("1234",), rows={"1234": actual}, policy=TechnicalFinancialPolicy()
    )
    card = technical_financial_screening_brief(saved.result)["cards"][0]
    assert card["financial_gate"]["income_attribution_context"] == context
    card["financial_gate"]["income_attribution_context"]["quarter"]["metrics"]["parent_profit"][
        "documents"
    ].clear()
    assert context["quarter"]["metrics"]["parent_profit"]["documents"]
    assert data == before


def test_incomparable_bases_and_different_metric_units_are_never_normalized():
    data = payloads()
    annual = current(data, "annual_ttm")
    annual.update(statement_basis="nonconsolidated", accounting_standard="IFRS")
    data["company_growth"]["metric_units"]["operating_profit"] = "USD"
    data["company_growth"]["metric_units"]["parent_profit"] = "JPY"
    actual = project(data)
    context = actual.financial_gate["income_attribution_context"]
    assert context["annual"]["statement_basis"] == "nonconsolidated"
    assert context["annual"]["accounting_standard"] == "IFRS"
    assert context["quarter"]["statement_basis"] == "consolidated"
    assert context["quarter"]["accounting_standard"] == "JP"
    assert context["metric_units"] == {"operating_profit": "USD", "parent_profit": "JPY"}
    assert actual.financial_gate["checks"]["annual_quarter_basis"]["state"] == "unavailable"
    assert "difference" not in context


@pytest.mark.parametrize("reason", ["not_observed", "known_restated_annual_operand_conflict"])
def test_metric_missingness_is_not_derived_from_other_income(reason):
    data = payloads()
    quarter = current(data, "quarter")
    quarter["metrics"]["parent_profit"] = {"value": None, "reason": reason, "documents": []}
    assert (
        project(data).financial_gate["income_attribution_context"]["quarter"]["metrics"][
            "parent_profit"
        ]
        == quarter["metrics"]["parent_profit"]
    )
    del quarter["metrics"]["parent_profit"]
    assert (
        "parent_profit"
        not in project(data).financial_gate["income_attribution_context"]["quarter"]["metrics"]
    )


def test_unknown_observations_and_raw_units_are_not_backfilled():
    data = payloads()
    del data["company_growth"]["metric_units"]
    assert project(data).financial_gate["income_attribution_context"]["metric_units"] == {}
    del data["company_growth"]
    context = project(data).financial_gate["income_attribution_context"]
    assert context["annual"] is None
    assert context["quarter"] is None


def test_v14_is_exact_read_only_without_income_context_backfill():
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method="technical-financial-discovery-v14"),
        "T",
        "G",
        (row("1234"),),
        ("1234",),
        {},
    )
    codec = TypeAdapter(TechnicalFinancialSelection)
    body = codec.dump_json(saved)
    restored = codec.validate_json(body)
    assert codec.dump_json(restored) == body
    assert (
        "income_attribution_context"
        not in technical_financial_screening_brief(restored)["cards"][0]["financial_gate"]
    )
    with pytest.raises(ValueError, match="historical"):
        evaluate_technical_financial_rows(metadata(), tickers=(), rows={}, policy=restored.policy)


def test_selected_full_fy_is_not_relabelled_as_latest_ttm():
    data = payloads()
    add_prior_fy(data)
    context = project(data).financial_gate["income_attribution_context"]
    assert context["annual_variant"] == "full_fy"
    assert context["annual"]["period_end"] == "2025-12-31"
    assert context["quarter"]["period_end"] == "2026-09-30"
