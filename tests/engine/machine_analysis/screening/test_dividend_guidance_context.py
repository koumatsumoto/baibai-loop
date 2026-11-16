"""会社配当予想は任意の比較材料。欠測を無配や安全性へ変えない。"""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_financial_growth import metadata, payloads
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


@pytest.mark.parametrize(
    ("dps", "status"),
    [(0, "confirmed"), (50, "confirmed"), (None, "missing"), (None, "unresolved")],
)
def test_guidance_amount_and_own_period_survive_missing_earnings_without_gate_change(dps, status):
    data = payloads()
    financial = data["company_financials"]["financial"]
    financial.update(
        dps_forecast_annual=dps,
        forecast_profit_yen=None,
        earnings_forecast_source={"status": "missing"},
        dps_actual_annual=100,
        dividend_split_factor=0.5,
        dividend_yield=0.2,
    )
    source = {
        "period_start": "2026-04-01",
        "period_end": "2027-03-31",
        "status": status,
        "basis": "issuer_dividend",
        "target_basis": "issuer_dividend",
        "disclosed_at": "2026-10-30",
        "disclosure_number": "synthetic",
        "dps_forecast_annual_source_records": "synthetic",
    }
    financial["dividend_forecast_source"] = source
    before = deepcopy(data)
    actual = project(data)
    expected = {"dps_forecast_annual": dps, "dividend_forecast_source": source}
    assert actual.financial_gate["dividend_forecast_context"] == expected
    assert actual.financial_gate["state"] == "research_candidate"
    saved = evaluate_technical_financial_rows(
        metadata(), tickers=("1234",), rows={"1234": actual}, policy=TechnicalFinancialPolicy()
    )
    card = technical_financial_screening_brief(saved.result)["cards"][0]
    assert card["financial_gate"]["dividend_forecast_context"] == expected
    card["financial_gate"]["dividend_forecast_context"]["dividend_forecast_source"]["status"] = (
        "changed"
    )
    assert (
        actual.financial_gate["dividend_forecast_context"]["dividend_forecast_source"]["status"]
        == status
    )
    assert data == before


def test_absent_guidance_does_not_use_actual_dividend_or_split_factor():
    data = payloads()
    data["company_financials"]["financial"].update(
        dps_actual_annual=50, dividend_split_factor=0.5, dividend_yield=0.1
    )
    assert project(data).financial_gate["dividend_forecast_context"] == {}
    del data["company_financials"]
    assert "dividend_forecast_context" not in project(data).financial_gate


def test_explicit_null_source_is_not_filled_from_earnings_source():
    data = payloads()
    data["company_financials"]["financial"].update(
        dps_forecast_annual=50,
        dividend_forecast_source=None,
        earnings_forecast_source={"period_end": "2027-03-31", "status": "confirmed"},
    )
    assert project(data).financial_gate["dividend_forecast_context"] == {
        "dps_forecast_annual": 50,
        "dividend_forecast_source": None,
    }


def test_v16_absence_remains_exact_without_guidance_backfill():
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method="technical-financial-discovery-v16"),
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
        "dividend_forecast_context"
        not in technical_financial_screening_brief(restored)["cards"][0]["financial_gate"]
    )
    with pytest.raises(ValueError, match="historical"):
        evaluate_technical_financial_rows(metadata(), tickers=(), rows={}, policy=restored.policy)
