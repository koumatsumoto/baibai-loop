"""利益の成長と資金化の事実を混ぜず、期付きCFを初回比較まで保持する。"""

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
    discovery_display_order,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    evaluate_technical_financial_rows,
    project_technical_financial_row,
)

FIELD = "cash_flow_context"
CASH_FLOW = {
    "cfo": -200.0,
    "cfo_source": {
        "period_start": "2026-04-01",
        "period_end": "2026-09-30",
        "period_type": "2Q",
        "accounting_standard": "JP",
        "statement_basis": "consolidated",
        "disclosed_at": "2026-11-13",
        "disclosed_time": "15:30:00",
        "disclosure_number": "synthetic-cash-flow",
        "source_trace": {"unknown_field": None},
    },
    "ocf_ttm": -500.0,
    "ocf_ttm_period": {
        "start": "2025-10-01",
        "end": "2026-09-30",
        "method": "fy_plus_current_ytd_minus_prior_ytd",
        "components": [{"amount_yen": -200.0, "coefficient": 1, "source_id": "synthetic"}],
    },
    "ttm_quality_ocf_yield": "unavailable",
}


def project(data):
    return project_technical_financial_row("1234", models=models(data), metadata=metadata())


@pytest.mark.parametrize(
    ("cfo", "ttm"), [(-200.0, -500.0), (200.0, 500.0), (0.0, 0.0), (None, None)]
)
def test_cashflow_and_its_period_reach_card_without_changing_profit_gates(cfo, ttm):
    data = payloads()
    control = project(data)
    context = {**deepcopy(CASH_FLOW), "cfo": cfo, "ocf_ttm": ttm}
    data["company_financials"]["financial"].update(context)
    before = deepcopy(data)
    actual = project(data)
    assert actual.financial_gate[FIELD] == context
    assert actual.financial_gate["state"] == "research_candidate"
    assert actual.financial_gate["checks"]["positive_quarter_operating_profit"]["value"] == 100.0
    assert {k: v for k, v in actual.financial_gate.items() if k != FIELD} == {
        k: v for k, v in control.financial_gate.items() if k != FIELD
    }
    assert actual.technical == control.technical
    assert actual.financial_paths == control.financial_paths
    assert actual.screening_lane == control.screening_lane
    assert discovery_display_order((actual,)) == discovery_display_order((control,))
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(), "T", "G", (actual,), ("1234",), {}
    )
    card = technical_financial_screening_brief(saved)["cards"][0]
    assert card["financial_gate"][FIELD] == context
    card["financial_gate"][FIELD]["cfo_source"]["source_trace"].clear()
    card["financial_gate"][FIELD]["ocf_ttm_period"]["components"].clear()
    assert actual.financial_gate[FIELD] == context
    assert data == before


@pytest.mark.parametrize(
    "source", [None, {}, {"period_type": "FY", "unavailable_reason": "source_unresolved"}]
)
def test_partial_cf_and_unknown_period_are_not_completed_or_annualized(source):
    data = payloads()
    data["company_financials"]["financial"].update(cfo=123.0, cfo_source=source, ocf_ttm=None)
    context = project(data).financial_gate[FIELD]
    assert context == {"cfo": 123.0, "cfo_source": source, "ocf_ttm": None}
    assert "ocf_ttm_period" not in context


def test_unrecorded_and_ungenerated_cf_are_distinct():
    data = payloads()
    assert project(data).financial_gate[FIELD] == {}
    del data["company_financials"]
    actual = project(data)
    assert FIELD not in actual.financial_gate
    assert actual.financial_gate["state"] == "research_candidate"


@pytest.mark.parametrize("version", range(1, 9))
def test_old_policy_does_not_backfill_cf_or_regenerate(version):
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
    assert FIELD not in technical_financial_screening_brief(restored)["cards"][0]["financial_gate"]
    with pytest.raises(ValueError, match=r"historical.*read-only"):
        evaluate_technical_financial_rows(metadata(), tickers=(), rows={}, policy=restored.policy)
