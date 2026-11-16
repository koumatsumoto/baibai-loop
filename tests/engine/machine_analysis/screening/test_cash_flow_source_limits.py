"""EDINETの不足/不採用理由を別出典CFの無効化や値の補完にしない。"""

from copy import deepcopy
from dataclasses import asdict

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_cash_flow_composition_context import card
from tests.engine.machine_analysis.screening.test_cash_flow_context import CASH_FLOW, project
from tests.engine.machine_analysis.screening.test_financial_growth import metadata, payloads

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
    "reason",
    [
        None,
        "",
        "entity_scale_mismatch",
        "metric_unavailable:debt",
        "non_consolidated_fallback,metric_unavailable:capex,entity_scale_mismatch",
    ],
)
def test_edinet_source_limits_keep_raw_values_and_independent_cash_flow(reason):
    data = payloads()
    financial = data["company_financials"]["financial"]
    flows = {
        "amount_unit": "JPY",
        "amounts": {"operating_cash_flow": -300, "capex": None, "free_cash_flow": None},
        "source_doc_id": "synthetic-edinet",
        "consolidation_basis": "nonconsolidated",
        "measurement_period": {"start": None, "end": None, "reason": "fact_dates_not_retained"},
    }
    financial.update(deepcopy(CASH_FLOW), edinet_reported_flows=deepcopy(flows))
    baseline = project(data)
    assert "edinet_failure_reasons" not in baseline.financial_gate["cash_flow_context"]
    financial["edinet_failure_reasons"] = reason
    before = deepcopy(data)
    actual = project(data)
    context = card(actual)["financial_gate"]["cash_flow_context"]
    assert context == {
        **CASH_FLOW,
        "edinet_reported_flows": flows,
        "edinet_failure_reasons": reason,
    }
    # 同一rowの他の意味は不変更。短信CFはEDINETの不足へ巻き込まれない。
    without_added_reason = asdict(actual)
    without_added_reason["financial_gate"]["cash_flow_context"].pop("edinet_failure_reasons")
    assert without_added_reason == asdict(baseline)
    assert data == before
    context["edinet_failure_reasons"] = "changed"
    assert actual.financial_gate["cash_flow_context"]["edinet_failure_reasons"] == reason


def test_no_reported_flow_does_not_turn_edinet_failure_into_cash_flow_failure():
    data = payloads()
    data["company_financials"]["financial"].update(
        deepcopy(CASH_FLOW), edinet_reported_flows=None, edinet_failure_reasons="unsupported_source"
    )
    context = card(project(data))["financial_gate"]["cash_flow_context"]
    assert context == {
        **CASH_FLOW,
        "edinet_reported_flows": None,
        "edinet_failure_reasons": "unsupported_source",
    }


@pytest.mark.parametrize(
    "method", ["technical-financial-discovery-v20", "technical-financial-discovery-v21"]
)
def test_old_saved_cash_flow_is_not_backfilled_with_current_edinet_limits(method):
    old = project(payloads())
    old.financial_gate["cash_flow_context"] = deepcopy(CASH_FLOW)
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method=method),
        "T",
        "G",
        (old,),
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
