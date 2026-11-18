"""残高の欠測/時計/basisを保ち、銀行cashも余剰資金へ昇格させない。"""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_financial_growth import payloads
from tests.engine.machine_analysis.screening.test_growth_period_context import project
from tests.engine.machine_analysis.screening.test_technical_financial import row
from tests.helpers.archived_technical_financial import stored_result

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)


@pytest.mark.parametrize("basis", ["consolidated", "nonconsolidated"])
@pytest.mark.parametrize(
    ("debt", "reason"), [(None, "unsupported_debt_component:bank_borrowing"), (30, None)]
)
def test_saved_balances_reach_card_without_unknown_date_or_debt_repair(basis, debt, reason):
    data = payloads()
    balances = {
        "amount_unit": "JPY",
        "amounts": {"cash": 100, "debt": debt, "net_cash": None if debt is None else 70},
        "consolidation_basis": basis,
        "filing_fiscal_period": {"start": "2026-04-01", "end": "2027-03-31"},
        "measurement_date": {"date": None, "reason": "fact_date_not_retained"},
        "source_submit_datetime": "2026-11-01 12:00",
        "source_doc_id": "synthetic",
        "failure_reasons": reason,
        "interpretation": "reported_financial_balances_not_common_equity_surplus",
    }
    data["company_financials"]["financial"]["edinet_reported_balances"] = deepcopy(balances)
    before = deepcopy(data)
    actual = project(data)
    assert actual.financial_gate["reported_balance_context"] == balances
    assert actual.financial_gate["state"] == "research_candidate"
    saved = stored_result((actual,), order=("1234",))
    card = technical_financial_screening_brief(saved)["cards"][0]
    assert card["financial_gate"]["reported_balance_context"] == balances
    card["financial_gate"]["reported_balance_context"]["amounts"]["cash"] = 0
    assert actual.financial_gate["reported_balance_context"]["amounts"]["cash"] == 100
    assert data == before


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {
            "amount_unit": "USD",
            "amounts": {"cash": 1},
            "measurement_date": {"date": "2026-09-30", "reason": None},
        },
    ],
)
def test_raw_null_sparse_and_different_currency_remain_exact(value):
    data = payloads()
    data["company_financials"]["financial"]["edinet_reported_balances"] = value
    assert project(data).financial_gate["reported_balance_context"] == value


def test_no_reported_balance_does_not_use_flat_cash_or_invent_zero():
    data = payloads()
    assert "cash" in data["company_financials"]["financial"]
    assert "reported_balance_context" not in project(data).financial_gate
    del data["company_financials"]
    assert "reported_balance_context" not in project(data).financial_gate


def test_v15_absence_is_exact_read_only_without_current_model_backfill():
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method="technical-financial-discovery-v15"),
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
        "reported_balance_context"
        not in technical_financial_screening_brief(restored)["cards"][0]["financial_gate"]
    )
