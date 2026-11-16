"""選択済み簿価の出典を渡すだけで、現在の普通株資本やROEを補作しない。"""

from copy import deepcopy
from dataclasses import asdict

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_economic_path_inputs import (
    metadata,
    models,
    payloads,
)

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

BOOK = {
    "amount_yen": 20000.0,
    "method": "bps_times_current_shares",
    "measurement_date": "2025-06-30",
    "statement_basis": "consolidated",
    "accounting_standard": "JP",
    "disclosure_number": "synthetic-book-source",
    "disclosed_at": "2025-08-12",
    "disclosed_time": "15:00:00",
    "document_type": "2QFinancialStatements_Consolidated_JP",
    "operands": {"bps_yen": 200.0, "shares_ex_treasury": 100.0, "share_basis_as_of": "2026-01-05"},
}


def project(data):
    return project_technical_financial_row("1234", models=models(data), metadata=metadata())


def selection(row, *, policy=None):
    return TechnicalFinancialSelection(
        policy or TechnicalFinancialPolicy(), "T", "G", (row,), (row.ticker,), {}
    )


@pytest.mark.parametrize("method", ["bps_times_current_shares", "assets_times_equity_ratio"])
def test_selected_book_source_reaches_card_without_capital_or_gate_recalculation(method):
    data = payloads()
    book = deepcopy(BOOK)
    if method == "assets_times_equity_ratio":
        book.update(
            method=method,
            operands={"total_assets_yen": 40000.0, "equity_to_asset_ratio": 0.5},
            measurement_date="2025-12-31",
            statement_basis="nonconsolidated",
            accounting_standard=None,
        )
    financial = data["company_financials"]["financial"]
    financial.update(book_equity=book, same_state_equity_yen=999999.0, consolidation_basis="other")
    before = deepcopy(data)
    row = project(data)
    assert data == before
    card = technical_financial_screening_brief(selection(row), lane=row.screening_lane)["cards"][0]
    assert card["financial_paths"]["book_equity"] == book
    capital = next(
        item
        for item in card["financial_paths"]["coordinates"]
        if item["path_id"] == "capital_productivity"
    )
    assert capital["price_value"] == 2.0
    assert capital["business_value"] == pytest.approx(0.05)
    assert "not_verified_common_or_average_equity_roe" in capital["evidence"]["interpretation"]
    assert capital["evidence"]["earnings_component_periods"][0]["period_end"] == "2025-12-31"
    # 選択簿価の証拠が変わってもPBR座標や候補条件は補正しない。
    financial["book_equity"] = {**book, "amount_yen": 123.0, "measurement_date": None}
    changed = asdict(project(data))
    unchanged = asdict(row)
    assert changed["financial_paths"].pop("book_equity")["amount_yen"] == 123.0
    unchanged["financial_paths"].pop("book_equity")
    assert changed == unchanged
    card["financial_paths"]["book_equity"]["operands"].clear()
    assert row.financial_paths["book_equity"] == book
    assert before["company_financials"]["financial"]["book_equity"] == book


@pytest.mark.parametrize("present", [False, True])
def test_unrecorded_and_null_book_are_distinct_and_not_filled_from_other_equity(present):
    data = payloads()
    data["company_financials"]["financial"].update(
        same_state_equity_yen=1000.0,
        edinet_reported_balances={"net_assets": 2000.0},
    )
    if present:
        data["company_financials"]["financial"]["book_equity"] = None
    row = project(data)
    card = technical_financial_screening_brief(selection(row), lane=row.screening_lane)["cards"][0]
    assert ("book_equity" in row.financial_paths) is present
    assert ("book_equity" in card["financial_paths"]) is present
    if present:
        assert card["financial_paths"]["book_equity"] is None
    assert "book_equity" not in project({}).financial_paths


@pytest.mark.parametrize("version", range(1, 23))
def test_saved_old_book_absence_is_not_backfilled_or_recomputed(version):
    data = payloads()
    data["company_financials"]["financial"]["book_equity"] = deepcopy(BOOK)
    old = project(data)
    old.financial_paths.pop("book_equity")
    saved = selection(
        old, policy=TechnicalFinancialPolicy(method=f"technical-financial-discovery-v{version}")
    )
    codec = TypeAdapter(TechnicalFinancialSelection)
    original = codec.dump_json(saved)
    restored = codec.validate_json(original)
    assert codec.dump_json(restored) == original
    assert (
        "book_equity"
        not in technical_financial_screening_brief(restored, lane=old.screening_lane)["cards"][0][
            "financial_paths"
        ]
    )
    with pytest.raises(ValueError, match="historical"):
        evaluate_technical_financial_rows(metadata(), tickers=(), rows={}, policy=restored.policy)
