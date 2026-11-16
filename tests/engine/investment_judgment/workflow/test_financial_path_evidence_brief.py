"""財務座標の保存basisを表示し、reported profit/bookを検証済ROEにしない。"""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_technical_financial import row

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)


def selection(version, evidence):
    company = row("1234")
    company.financial_paths["matched_routes"] = ["capital_productivity"]
    company.financial_paths["paths"] = [
        {
            "path_id": "capital_productivity",
            "variant": "reported_profit_latest_book",
            "price_value": 0.8,
            "business_value": 0.1,
            "price_group": "bank:book_price",
            "business_group": "bank:reported_profit_book",
            "eligible": True,
            "evidence": deepcopy(evidence),
        }
    ]
    return TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method=f"technical-financial-discovery-v{version}"),
        "T",
        "G",
        (company,),
        ("1234",),
        {},
    )


@pytest.mark.parametrize("version", [1, 12, 16])
def test_exact_saved_capital_interpretation_and_periods_reach_card_without_snapshot_change(version):
    evidence = {
        "interpretation": "reported_profit_to_model_implied_book_not_verified_common_or_average_equity_roe",
        "book_provenance": "selected_model_book_in_context_financial_book_equity",
        "same_current_market_cap": True,
        "earnings_components": [
            {
                "coefficient": 1,
                "period_start": "2025-04-01",
                "period_end": "2026-03-31",
                "amount_yen": 10,
            }
        ],
    }
    saved = selection(version, evidence)
    codec = TypeAdapter(TechnicalFinancialSelection)
    before = codec.dump_json(saved)
    brief = technical_financial_screening_brief(saved)
    coordinate = brief["cards"][0]["financial_paths"]["coordinates"][0]
    assert coordinate["evidence"] == {
        k: v for k, v in evidence.items() if k != "earnings_components"
    } | {
        "earnings_component_periods": [
            {"coefficient": 1, "period_start": "2025-04-01", "period_end": "2026-03-31"}
        ]
    }
    assert coordinate["business_value"] == 0.1
    assert coordinate["price_value"] == 0.8
    assert "normalization" not in coordinate
    assert "score" not in coordinate
    coordinate["evidence"]["earnings_component_periods"][0]["coefficient"] = 9
    assert codec.dump_json(saved) == before


def test_mixed_annual_price_and_quarter_growth_periods_are_not_collapsed():
    annual = {
        "measurement": "annual_ttm",
        "period_start": "2025-10-01",
        "period_end": "2026-09-30",
        "statement_basis": "consolidated",
        "accounting_standard": "JP",
    }
    quarter = {
        "measurement": "quarter",
        "period_start": "2026-07-01",
        "period_end": "2026-09-30",
        "statement_basis": "nonconsolidated",
        "accounting_standard": "IFRS",
    }
    saved = selection(
        16,
        {"sales_observation": annual, "change_observation": quarter, "selected_variant": "quarter"},
    )
    c = technical_financial_screening_brief(saved)["cards"][0]["financial_paths"]["coordinates"][0]
    assert c["evidence"]["period_basis"] == [annual, quarter]
    assert c["evidence"]["selected_variant"] == "quarter"
    assert "same_period_and_statement_basis" not in c["evidence"]


def test_sparse_legacy_evidence_does_not_invent_new_basis_or_admit_ineligible_path():
    saved = selection(1, {})
    company = saved.rows[0]
    company.financial_paths["paths"].append(
        {"path_id": "earning_power", "eligible": False, "evidence": {"interpretation": "not shown"}}
    )
    c = technical_financial_screening_brief(saved)["cards"][0]["financial_paths"]["coordinates"]
    assert len(c) == 1
    assert c[0]["evidence"] == {}
