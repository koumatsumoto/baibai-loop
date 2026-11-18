"""薄い全社観測に使う入力と、個社を選んでから読む事実を区別する。"""

from copy import deepcopy

import pytest
from tests.engine.machine_analysis.screening.test_economic_path_inputs import (
    metadata,
    models,
    payloads,
)
from tests.helpers.discovery_source_examples import BOOK, CASH_FLOW, FORECAST, NOTICE

from baibai_engine.machine_analysis.screening.policies.discovery import project_discovery_row


def read(data):
    return project_discovery_row("1234", models=models(data), metadata=metadata())


def test_book_source_and_path_value_remain_distinct_from_raw_balance_and_forecast():
    data = payloads()
    financial = data["company_financials"]["financial"]
    financial.update(
        book_equity=deepcopy(BOOK),
        same_state_equity_yen=1000.0,
        edinet_reported_balances={"net_assets": 2000.0},
    )
    row = read(data)
    assert row["book_equity"] == {
        "amount_yen": 20000.0,
        "method": "bps_times_current_shares",
        "measurement_date": "2025-06-30",
        "statement_basis": "consolidated",
        "disclosed_at": "2025-08-12",
    }
    capital = next(p for p in row["coordinates"] if p["path_id"] == "capital_productivity")
    assert (capital["price_value"], capital["business_value"]) == (2.0, 0.05)
    # Unequal source amounts and later cash/profit/notice must not replace that coordinate.
    financial.update(**CASH_FLOW, **FORECAST, accounting_change_context=NOTICE)
    before = deepcopy(data)
    assert read(data) == row
    assert data == before
    for amount in (None, 0, -20):
        financial["book_equity"]["amount_yen"] = amount
        assert read(data)["book_equity"] == {**row["book_equity"], "amount_yen": amount}
    del financial["book_equity"]
    assert read(data)["book_equity"] is None


@pytest.mark.parametrize(
    ("fields", "adjustments", "expected"),
    [
        ({}, {}, None),
        ({"capital_action_evidence": None}, {}, (None, None)),
        ({"capital_action_evidence": []}, {}, (0, None)),
        ({}, {"share_basis_adjustments": []}, (None, 0)),
        (
            {
                "capital_action_evidence": [{"issued_shares": 80, "effective_date": "2026-11-20"}]
                * 2
            },
            {"share_basis_adjustments": []},
            (2, 0),
        ),
    ],
)
def test_action_observation_counts_are_not_inferred_share_or_right_evidence(
    fields, adjustments, expected
):
    data = payloads()
    financial = data["company_financials"]
    financial["earnings_price"].pop("share_basis_adjustments", None)
    baseline = read(data)
    financial.update(fields)
    financial["earnings_price"].update(adjustments)
    result = read(data)
    assert result == {
        **baseline,
        "capital_action": (
            None
            if expected is None
            else {"observations": expected[0], "share_basis_adjustments": expected[1]}
        ),
    }
