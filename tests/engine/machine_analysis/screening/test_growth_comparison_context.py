"""採用済み前年比の母数・不足を初回比較へ保ち、増減額で規模を順位付けしない。"""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_financial_growth import (
    change,
    metadata,
    models,
    payloads,
)
from tests.engine.machine_analysis.screening.test_growth_period_context import project
from tests.engine.machine_analysis.screening.test_technical_financial import row

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies.financial_growth import (
    evaluate_financial_growth_gate,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    evaluate_technical_financial_rows,
)


def test_selected_comparisons_reach_card_exactly_and_independently():
    data = payloads()
    prior = deepcopy(data)
    diagnostic = evaluate_financial_growth_gate("1234", models=models(data), metadata=metadata())
    projected = project(data)
    context = projected.financial_gate["quarter_growth_context"]
    assert context["changes"] == diagnostic["observations"]["quarter_changes"]
    assert context["metric_units"] == {
        "sales": "JPY",
        "operating_profit": "JPY",
        "operating_margin": "ratio",
    }
    assert context["comparison_basis"] == "source_reported_economic_scope_unverified"
    result = evaluate_technical_financial_rows(
        metadata(), tickers=("1234",), rows={"1234": projected}, policy=TechnicalFinancialPolicy()
    ).result
    card = technical_financial_screening_brief(result)["cards"][0]
    assert card["financial_gate"]["quarter_growth_context"] == context
    card["financial_gate"]["quarter_growth_context"]["changes"]["sales"]["from_value"] = 0
    assert context["changes"]["sales"]["from_value"] == 900
    assert data == prior


@pytest.mark.parametrize(
    ("previous", "current", "rate", "reason"),
    [
        (-100, -50, None, "nonpositive_denominator"),
        (-100, 50, None, "nonpositive_denominator"),
        (0, 50, None, "nonpositive_denominator"),
        (100, 150, 0.5, None),
        (None, 50, None, "operand_unavailable"),
    ],
)
def test_signed_or_missing_operands_are_not_recomputed(previous, current, rate, reason):
    data = payloads()
    selected = change(data, "operating_profit")
    selected.update(
        from_value=previous,
        to_value=current,
        difference=None if previous is None else current - previous,
        rate=rate,
        reason=reason,
    )
    context = project(data).financial_gate["quarter_growth_context"]
    assert context["changes"]["operating_profit"] == selected
    assert context["changes"]["operating_profit"]["rate"] == rate


def test_missing_raw_units_and_missing_growth_are_not_backfilled():
    data = payloads()
    del data["company_growth"]["metric_units"]
    assert project(data).financial_gate["quarter_growth_context"]["metric_units"] == {}
    del data["company_growth"]
    context = project(data).financial_gate["quarter_growth_context"]
    assert context["changes"] == {
        name: {} for name in ("sales", "operating_profit", "operating_margin")
    }
    assert context["metric_units"] == {}


def test_v13_preserves_existing_periods_but_does_not_invent_comparisons():
    old_row = row("1234")
    old_row.financial_gate["observation_period_context"] = {"annual_variant": "annual_ttm"}
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method="technical-financial-discovery-v13"),
        "T",
        "G",
        (old_row,),
        ("1234",),
        {},
    )
    codec = TypeAdapter(TechnicalFinancialSelection)
    body = codec.dump_json(saved)
    restored = codec.validate_json(body)
    assert codec.dump_json(restored) == body
    card = technical_financial_screening_brief(restored)["cards"][0]["financial_gate"]
    assert "quarter_growth_context" not in card
    assert card["observation_period_context"] == {"annual_variant": "annual_ttm"}
    with pytest.raises(ValueError, match="historical"):
        evaluate_technical_financial_rows(metadata(), tickers=(), rows={}, policy=restored.policy)
