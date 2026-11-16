"""初回比較で異なる決算期・資料日を隠さず、鮮度scoreや新しいgateにしない。"""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_financial_growth import metadata, models, payloads
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
    project_technical_financial_row,
)


def project(data):
    return project_technical_financial_row("1234", models=models(data), metadata=metadata())


def test_adopted_periods_and_all_source_dates_reach_first_card_without_value_changes():
    data = payloads()
    before = deepcopy(data)
    actual = project(data)
    context = actual.financial_gate["observation_period_context"]
    assert context["annual_variant"] == "annual_ttm"
    assert context["quarter"] == {
        "measurement": "quarter",
        "period_start": "2026-07-01",
        "period_end": "2026-09-30",
        "statement_basis": "consolidated",
        "accounting_standard": "JP",
        "period_status": "available",
        "source_disclosure_dates": ["2026-10-30"],
    }
    assert context["annual"]["period_start"] == "2025-10-01"
    assert context["balance"]["period_start"] == "2026-09-30"
    saved = evaluate_technical_financial_rows(
        metadata(), tickers=("1234",), rows={"1234": actual}, policy=TechnicalFinancialPolicy()
    )
    assert saved.result.policy.method == "technical-financial-discovery-v26"
    card = technical_financial_screening_brief(saved.result)["cards"][0]
    assert card["financial_gate"]["observation_period_context"] == context
    assert actual.financial_gate["checks"]["positive_quarter_operating_profit"]["value"] == 100
    assert actual.financial_gate["state"] == "research_candidate"
    card["financial_gate"]["observation_period_context"]["quarter"][
        "source_disclosure_dates"
    ].clear()
    assert context["quarter"]["source_disclosure_dates"] == ["2026-10-30"]
    assert data == before


@pytest.mark.parametrize("dates", [[], ["2026-10-10"], ["2026-10-10", "2026-10-30"]])
def test_missing_dates_are_not_filled_and_multiple_source_dates_are_not_one_latest_clock(dates):
    data = payloads()
    quarter = data["company_growth"]["periods"][0]["observations"][0]
    for metric in quarter["metrics"].values():
        source = metric["documents"][0]
        metric["documents"] = [{**source, "disclosed_at": day} for day in dates]
    context = project(data).financial_gate["observation_period_context"]
    assert context["quarter"]["source_disclosure_dates"] == dates
    assert "point_in_time" in context["read_semantics"]
    assert "published_at" not in context["quarter"]


def test_missing_growth_stays_unknown_without_using_generation_date_as_period():
    data = payloads()
    del data["company_growth"]
    context = project(data).financial_gate["observation_period_context"]
    assert context["quarter"] is None
    assert context["annual"] is None
    assert context["balance"] is None


@pytest.mark.parametrize("version", range(1, 13))
def test_old_selection_without_period_context_is_exact_and_cannot_be_regenerated(version):
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method=f"technical-financial-discovery-v{version}"),
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
        "observation_period_context"
        not in technical_financial_screening_brief(restored)["cards"][0]["financial_gate"]
    )
    with pytest.raises(ValueError, match="historical"):
        evaluate_technical_financial_rows(metadata(), tickers=(), rows={}, policy=restored.policy)
