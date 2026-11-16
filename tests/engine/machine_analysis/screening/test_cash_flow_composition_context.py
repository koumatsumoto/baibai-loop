"""部分的な報告CF内訳を正常/株主余剰CFへ置き換えず初回比較に残す。"""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_cash_flow_context import CASH_FLOW, project
from tests.engine.machine_analysis.screening.test_financial_growth import metadata, payloads
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
)


def card(actual):
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(), "T", "G", (actual,), ("1234",), {}
    )
    return technical_financial_screening_brief(saved)["cards"][0]


@pytest.mark.parametrize(
    "flows",
    [
        None,
        {},
        {
            "amount_unit": "USD",
            "amounts": {
                "operating_cash_flow": 500,
                "ocf_receivables_cash_effect": -30,
                "ocf_inventories_cash_effect": None,
                "ocf_payables_cash_effect": 0,
                "capex_ppe_reported": 10,
                "capex_intangible_reported": None,
                "capex": None,
                "free_cash_flow": None,
            },
            "period_kind": "interim",
            "measurement_period": {"start": None, "end": None, "reason": "fact_dates_not_retained"},
            "filing_fiscal_period": {"start": "2026-04-01", "end": "2027-03-31"},
            "consolidation_basis": "nonconsolidated",
            "source_doc_id": "synthetic",
            "annual_quality": {"free_cash_flow": "unavailable"},
            "annual_quality_basis": "annual_filing_metadata_not_native_fact_dates",
        },
    ],
)
def test_reported_components_preserve_partial_missing_unit_period_and_basis(flows):
    data = payloads()
    financial = data["company_financials"]["financial"]
    financial.update(deepcopy(CASH_FLOW))
    baseline = project(data)
    financial.update(edinet_reported_flows=flows, edinet_capex_source=None)
    before = deepcopy(data)
    actual = project(data)
    shown = card(actual)["financial_gate"]["cash_flow_context"]
    assert shown == {**CASH_FLOW, "edinet_reported_flows": flows, "edinet_capex_source": None}
    # 別期間JPY/連結の既存OCFと、期間不明USD/単体の部分値を接続計算しない。
    assert shown["ocf_ttm"] == CASH_FLOW["ocf_ttm"]
    assert actual.financial_gate["checks"] == baseline.financial_gate["checks"]
    assert actual.financial_gate["routes"] == baseline.financial_gate["routes"]
    assert actual.financial_paths == baseline.financial_paths
    assert discovery_display_order((actual,)) == discovery_display_order((baseline,))
    shown["edinet_reported_flows"] = {"changed": True}
    assert actual.financial_gate["cash_flow_context"]["edinet_reported_flows"] == flows
    assert data == before


def test_recorded_capex_source_is_not_invented_from_partial_components():
    data = payloads()
    financial = data["company_financials"]["financial"]
    financial["edinet_reported_flows"] = {
        "amounts": {"capex_ppe_reported": 12, "capex": None, "free_cash_flow": None}
    }
    actual = project(data)
    context = card(actual)["financial_gate"]["cash_flow_context"]
    assert "edinet_capex_source" not in context
    assert context["edinet_reported_flows"]["amounts"]["capex"] is None
    financial["edinet_capex_source"] = "purchase_of_fixed_assets"
    assert (
        project(data).financial_gate["cash_flow_context"]["edinet_capex_source"]
        == "purchase_of_fixed_assets"
    )


@pytest.mark.parametrize("sector", ["銀行業", "情報・通信業"])
def test_reported_negative_cash_does_not_normalize_or_change_sector_gate(sector):
    data = payloads()
    data["market_context"] = {"master": {"sector_33": sector}}
    baseline = project(data)
    data["company_financials"]["financial"].update(
        edinet_reported_flows={
            "amount_unit": "JPY",
            "amounts": {"operating_cash_flow": -1000, "free_cash_flow": -1100},
            "period_kind": "annual",
        }
    )
    actual = project(data)
    assert actual.sector == sector
    assert actual.financial_gate["checks"] == baseline.financial_gate["checks"]
    assert actual.financial_gate["state"] == baseline.financial_gate["state"]
    assert (
        card(actual)["financial_gate"]["cash_flow_context"]["edinet_reported_flows"]["amounts"][
            "free_cash_flow"
        ]
        == -1100
    )


def test_old_saved_cf_is_not_extended_with_reported_components():
    legacy = row("1234")
    legacy.financial_gate["cash_flow_context"] = deepcopy(CASH_FLOW)
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method="technical-financial-discovery-v18"),
        "T",
        "G",
        (legacy,),
        ("1234",),
        {},
    )
    codec = TypeAdapter(TechnicalFinancialSelection)
    original = codec.dump_json(saved)
    restored = codec.validate_json(original)
    assert codec.dump_json(restored) == original
    assert (
        technical_financial_screening_brief(restored)["cards"][0]["financial_gate"][
            "cash_flow_context"
        ]
        == CASH_FLOW
    )
    with pytest.raises(ValueError, match="historical"):
        evaluate_technical_financial_rows(metadata(), tickers=(), rows={}, policy=restored.policy)


def test_unrecorded_or_missing_model_does_not_create_reported_flows():
    data = payloads()
    assert project(data).financial_gate["cash_flow_context"] == {}
    del data["company_financials"]
    assert "cash_flow_context" not in project(data).financial_gate
