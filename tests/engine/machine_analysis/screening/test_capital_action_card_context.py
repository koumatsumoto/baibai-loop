"""資本変更の保存根拠は現在株数・将来希薄化の認定ではない。"""

from copy import deepcopy
from dataclasses import replace

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


def earnings(**fields):
    return {
        "as_of": "2026-11-14",
        "price_date": None,
        "price_yen": None,
        "shares_ex_treasury": None,
        "market_cap_yen": None,
        "profit_ttm_yen": None,
        "statement_basis": None,
        "profit_components": [],
        "earnings_yield": None,
        "unavailable_reason": None,
        **fields,
    }


def brief(actual):
    saved = evaluate_technical_financial_rows(
        metadata(), tickers=("1234",), rows={"1234": actual}, policy=TechnicalFinancialPolicy()
    )
    return technical_financial_screening_brief(saved.result)["cards"][0]


@pytest.mark.parametrize(
    "observations",
    [
        None,
        [],
        [
            {
                "description": "completed_common_share_conversion_total",
                "issued_shares": 130,
                "effective_date": "2026-10-01",
                "published_on": "2026-09-30",
                "received_at": "2026-10-02T00:00:00Z",
                "disclosure_number": "synthetic",
            }
        ],
    ],
)
def test_raw_action_evidence_preserves_null_empty_and_clocks_without_changing_gate(observations):
    data = payloads()
    baseline = project(data)
    data["company_financials"]["capital_action_evidence"] = observations
    before = deepcopy(data)
    actual = project(data)
    assert actual.financial_gate == baseline.financial_gate
    assert actual.screening_lane == baseline.screening_lane
    expected = {"observations": observations}
    assert actual.financial_paths["capital_action_context"] == expected
    shown = brief(actual)["financial_paths"]["capital_action_context"]
    assert shown["observations"] == observations
    assert ("observations_omitted_count" in shown) == isinstance(observations, list)
    assert "earnings_price_unavailable_reason" not in shown
    assert "share_basis_adjustments" not in shown
    assert data == before


def test_raw_adjustments_and_reason_are_not_reconstructed_from_shares():
    data = payloads()
    data["company_financials"]["earnings_price"] = earnings(
        share_basis_adjustments=[], unavailable_reason="current_treasury_unverified"
    )
    actual = project(data)
    assert actual.financial_paths["capital_action_context"] == {
        "share_basis_adjustments": [],
        "earnings_price_unavailable_reason": "current_treasury_unverified",
    }
    assert "observations" not in brief(actual)["financial_paths"]["capital_action_context"]


@pytest.mark.parametrize(
    "reason", ["unavailable_annual_profit", "stale_price", "future_reason", None]
)
def test_earnings_reason_does_not_claim_that_positive_capital_is_unavailable(reason):
    data = payloads()
    data["company_financials"].update(
        expected_price_date="2026-11-13",
        earnings_price=earnings(
            price_date="2026-11-12" if reason == "stale_price" else "2026-11-13",
            price_yen=10.0,
            shares_ex_treasury=80.0,
            market_cap_yen=800.0,
            profit_ttm_yen=100.0 if reason == "stale_price" else None,
        ),
    )
    baseline = project(data)
    data["company_financials"]["earnings_price"]["unavailable_reason"] = reason
    before = deepcopy(data)
    actual = project(data)
    assert actual.financial_gate == baseline.financial_gate
    assert actual.technical == baseline.technical
    assert actual.screening_lane == baseline.screening_lane
    assert actual.financial_paths["paths"] == baseline.financial_paths["paths"]
    context = actual.financial_paths["capital_action_context"]
    assert context["earnings_price_unavailable_reason"] == reason
    assert "capital_basis_reason" not in context
    assert brief(actual)["financial_paths"]["capital_action_context"] == context
    assert data == before


@pytest.mark.parametrize("version", [17, 24, 25])
def test_historical_reason_label_is_read_without_migration_or_current_reevaluation(version):
    old_context = {"capital_basis_reason": "unavailable_annual_profit"}
    old_row = replace(
        row("1234"),
        financial_paths={
            "matched_routes": [],
            "paths": [],
            "capital_action_context": old_context,
        },
    )
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method=f"technical-financial-discovery-v{version}"),
        "T",
        "G",
        (old_row,),
        ("1234",),
        {},
    )
    codec = TypeAdapter(TechnicalFinancialSelection)
    body = codec.dump_json(saved)
    restored = codec.validate_json(body)
    shown = technical_financial_screening_brief(restored)["cards"][0]["financial_paths"]
    assert shown["capital_action_context"] == old_context
    assert "earnings_price_unavailable_reason" not in shown["capital_action_context"]
    assert codec.dump_json(restored) == body
    with pytest.raises(ValueError, match="historical"):
        evaluate_technical_financial_rows(metadata(), tickers=(), rows={}, policy=restored.policy)


def test_first_card_limits_each_list_without_dedup_or_current_share_inference():
    data = payloads()
    evidence = [{"issued_shares": 130, "received_at": str(i)} for i in range(5)]
    data["company_financials"].update(
        capital_action_evidence=evidence,
        earnings_price=earnings(share_basis_adjustments=[{"factor": i} for i in range(4)]),
    )
    actual = project(data)
    shown = brief(actual)["financial_paths"]["capital_action_context"]
    assert shown["observations"] == evidence[:3]
    assert shown["observations_omitted_count"] == 2
    assert shown["share_basis_adjustments_omitted_count"] == 1
    shown["observations"][0]["issued_shares"] = 999
    assert actual.financial_paths["capital_action_context"]["observations"] == evidence
    assert "detail_read" in technical_financial_screening_brief(
        TechnicalFinancialSelection(TechnicalFinancialPolicy(), "T", "G", (actual,), ("1234",), {})
    )


def test_old_absence_and_serialization_are_not_backfilled():
    data = payloads()
    assert "capital_action_context" not in project(data).financial_paths
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method="technical-financial-discovery-v17"),
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
        "capital_action_context"
        not in technical_financial_screening_brief(restored)["cards"][0]["financial_paths"]
    )
    with pytest.raises(ValueError, match="historical"):
        evaluate_technical_financial_rows(metadata(), tickers=(), rows={}, policy=restored.policy)
