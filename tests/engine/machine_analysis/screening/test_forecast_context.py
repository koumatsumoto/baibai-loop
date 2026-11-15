"""会社予想の反証は実績gateや利益推定に変換せず、初回比較まで保持する。"""

from copy import deepcopy
from dataclasses import replace

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
    discovery_display_order,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    evaluate_technical_financial_rows,
    project_technical_financial_row,
)

FORECAST = {
    "forecast_profit_yen": -1000.0,
    "forecast_ordinary_profit_yen": 1500.0,
    "forecast_full_year_loss_flag": True,
    "earnings_forecast_source": {
        "status": "confirmed",
        "period_start": "2026-06-01",
        "period_end": "2027-05-31",
        "basis": "consolidated",
        "target_basis": "consolidated",
        "disclosed_at": "2026-10-15",
        "disclosed_time": "15:30:00",
        "disclosure_number": "synthetic-forecast",
        "forecast_profit_source_records": "synthetic-forecast",
        "scope_trace": {"unverified_detail": None},
    },
}


def project(data):
    return project_technical_financial_row("1234", models=models(data), metadata=metadata())


@pytest.mark.parametrize(
    ("profit", "ordinary", "flag"),
    [
        (-1000.0, 1500.0, True),
        (-1000.0, 1500.0, False),
        (0.0, -1500.0, True),
        (0.0, 0.0, False),
        (1000.0, 1500.0, False),
        (None, None, None),
    ],
)
def test_saved_forecast_reaches_card_without_changing_checks_paths_or_order(profit, ordinary, flag):
    data = payloads()
    forecast = {
        **deepcopy(FORECAST),
        "forecast_profit_yen": profit,
        "forecast_ordinary_profit_yen": ordinary,
        "forecast_full_year_loss_flag": flag,
    }
    control = project(data)
    data["company_financials"]["financial"].update(forecast)
    before = deepcopy(data)
    actual = project(data)
    assert actual.financial_gate["forecast_context"] == forecast
    assert actual.financial_gate["state"] == "research_candidate"
    assert actual.financial_gate["checks"]["positive_quarter_operating_profit"]["value"] == 100.0
    for key in control.financial_gate.keys() - {"forecast_context"}:
        assert actual.financial_gate[key] == control.financial_gate[key]
    assert actual.technical == control.technical
    assert actual.financial_paths == control.financial_paths
    assert actual.screening_lane == control.screening_lane
    assert discovery_display_order((actual,)) == discovery_display_order((control,))
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(), "T", "G", (actual,), ("1234",), {}
    )
    card = technical_financial_screening_brief(saved)["cards"][0]
    assert card["financial_gate"]["forecast_context"] == forecast
    card["financial_gate"]["forecast_context"]["earnings_forecast_source"]["scope_trace"].clear()
    assert actual.financial_gate["forecast_context"] == forecast
    assert data == before


@pytest.mark.parametrize(
    "source", [None, {}, {"status": "unresolved", "reason": "conflicting_scope"}]
)
def test_unknown_source_is_preserved_not_completed_from_growth_or_generation(source):
    data = payloads()
    data["company_financials"]["financial"].update(
        forecast_profit_yen=None, forecast_full_year_loss_flag=None, earnings_forecast_source=source
    )
    forecast = project(data).financial_gate["forecast_context"]
    assert forecast == {
        "forecast_profit_yen": None,
        "forecast_full_year_loss_flag": None,
        "earnings_forecast_source": source,
    }
    assert "forecast_ordinary_profit_yen" not in forecast


def test_unrecorded_forecast_and_ungenerated_financial_model_remain_distinct():
    data = payloads()
    assert project(data).financial_gate["forecast_context"] == {}
    del data["company_financials"]
    missing = project(data)
    assert "forecast_context" not in missing.financial_gate
    assert missing.financial_gate["missing_models"] == ["company_financials"]
    assert missing.financial_gate["state"] == "research_candidate"


@pytest.mark.parametrize("version", range(1, 6))
def test_old_policy_keeps_absent_context_canonical_and_cannot_regenerate(version):
    legacy = row("1234")
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method=f"technical-financial-discovery-v{version}"),
        "T",
        "G",
        (legacy,),
        ("1234",),
        {},
    )
    codec = TypeAdapter(TechnicalFinancialSelection)
    raw = codec.dump_json(saved)
    restored = codec.validate_json(raw)
    assert codec.dump_json(restored) == raw
    assert (
        "forecast_context"
        not in technical_financial_screening_brief(restored)["cards"][0]["financial_gate"]
    )
    assert (
        "forecast_context"
        not in technical_financial_screening_brief(
            restored, page=None, comparison_tickers=("1234",)
        )["comparison_rows"][0]["financial_gate"]
    )
    with pytest.raises(ValueError, match=r"historical.*read-only"):
        evaluate_technical_financial_rows(metadata(), tickers=(), rows={}, policy=restored.policy)


@pytest.mark.parametrize(
    "lane", ["growth_research", "input_review", "alternative_financial_research"]
)
def test_current_policy_preserves_context_in_each_lane_without_reordering(lane):
    original = row("1234")
    first = replace(
        original,
        screening_lane=lane,
        financial_gate={**original.financial_gate, "forecast_context": deepcopy(FORECAST)},
    )
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(), "T", "G", (first,), ("1234",), {}
    )
    assert saved.policy.method == "technical-financial-discovery-v8"
    brief = technical_financial_screening_brief(saved, lane=lane)
    assert brief["cards"][0]["financial_gate"]["forecast_context"] == FORECAST
    if lane == "input_review":
        assert (
            brief["presentation"]["ties"] == "stable_company_hash_then_ticker_not_quality_or_return"
        )
