"""既存研究CLIの意味・表示を、候補政策ownerへの移動後も保つ。"""

from copy import deepcopy

import pytest
from tests.engine.machine_analysis.screening.test_financial_growth import (
    add_prior_quarter,
    change,
    current,
    mark_financial_margin_inapplicable,
    metadata,
    models,
    payloads,
)
from tools.experiments.financial_growth_gate import (
    POLICY_ID,
    evaluate_financial_growth_gate,
)

from baibai_engine.machine_analysis.screening.policies import financial_growth


@pytest.mark.parametrize(
    "case", ["growth", "recovery", "not_selected", "missing", "financial_na", "operand_reason"]
)
def test_research_wrapper_adds_only_its_original_adoption_label(case):
    data = payloads()
    if case == "recovery":
        current(data, "annual_ttm")["metrics"]["operating_profit"]["value"] = -100.0
    elif case == "not_selected":
        current(data, "balance")["metrics"]["net_assets"]["value"] = -1.0
    elif case == "missing":
        data.clear()
    elif case == "financial_na":
        mark_financial_margin_inapplicable(data, missing_op=True)
    elif case == "operand_reason":
        change(data, "operating_profit").update(
            from_value=None, difference=None, reason="operand_unavailable"
        )
        add_prior_quarter(data)["metrics"]["operating_profit"].update(
            value=None, reason="inconsistent_cumulative_revision"
        )
    before = deepcopy(data)
    args = {"models": models(data), "metadata": metadata()}
    core = financial_growth.evaluate_financial_growth_gate("1234", **args)
    research = evaluate_financial_growth_gate("1234", **args)
    assert research.pop("adoption_status") == "unadopted_research_only"
    assert research == core
    assert POLICY_ID == financial_growth.POLICY_ID == "financial-growth-research-trial-v1"
    assert data == before
    assert "action" not in core


def test_research_entry_delegates_without_a_second_policy(monkeypatch):
    saved = {"policy_id": POLICY_ID, "state": "input_unresolved", "checks": {}}
    original = deepcopy(saved)
    fixed_models = models(payloads())
    fixed_metadata = metadata()
    calls = []

    def evaluate(ticker, *, models, metadata):
        calls.append((ticker, models, metadata))
        return saved

    monkeypatch.setattr(financial_growth, "evaluate_financial_growth_gate", evaluate)
    result = evaluate_financial_growth_gate("1234", models=fixed_models, metadata=fixed_metadata)
    assert calls == [("1234", fixed_models, fixed_metadata)]
    assert result == {**saved, "adoption_status": "unadopted_research_only"}
    assert saved == original
