from __future__ import annotations

from copy import deepcopy

import pytest

from baibai_engine.foundation.review_set import (
    ReviewSetResolutionError,
    resolve_review_set_rows,
)


def _core_row(ticker: str = "2331") -> dict[str, object]:
    return {
        "ticker": ticker,
        "name": "Example",
        "expected_return_pct": 12.0,
        "fair_value_anchor_yen": 1300.0,
        "market_price_yen": 1000.0,
        "liquidity_status": "pass",
        "estimate_snapshot": {"er_annual": 0.12},
        "opportunity_lane_id": "value-carry",
    }


def test_duplicate_source_row_fails_closed() -> None:
    row = _core_row()
    with pytest.raises(ReviewSetResolutionError, match="duplicate ticker 2331"):
        resolve_review_set_rows({"review_tickers": ["2331"], "longlist": [row, deepcopy(row)]})


def test_review_ticker_without_source_row_fails_closed() -> None:
    with pytest.raises(ReviewSetResolutionError, match="must equal longlist in order"):
        resolve_review_set_rows({"review_tickers": ["2331"], "longlist": []})


def test_adopted_core_only_contract_requires_the_complete_longlist_in_order() -> None:
    first = _core_row("2331")
    second = _core_row("0001")

    tickers, rows = resolve_review_set_rows(
        {"review_tickers": ["2331", "0001"], "longlist": [first, second]}
    )

    assert tickers == ("2331", "0001")
    assert rows == {"2331": first, "0001": second}
    with pytest.raises(ReviewSetResolutionError, match="must equal longlist in order"):
        resolve_review_set_rows({"review_tickers": ["0001", "2331"], "longlist": [first, second]})
