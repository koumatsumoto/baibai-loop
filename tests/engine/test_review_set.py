from __future__ import annotations

from copy import deepcopy

import pytest

from baibai_engine.screening.selection.review_set import (
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


def _alt_row(ticker: str = "2331", *, overlap: bool) -> dict[str, object]:
    row = _core_row(ticker)
    row["opportunity_lane_id"] = "earnings-power"
    row["overlaps_value_carry_longlist"] = overlap
    return row


def test_overlap_resolves_to_value_carry_source_when_common_facts_match() -> None:
    core = _core_row()
    alt = _alt_row(overlap=True)
    tickers, rows = resolve_review_set_rows(
        {"review_tickers": ["2331"], "longlist": [core], "longlist_alt": {"entries": [alt]}}
    )

    assert tickers == ("2331",)
    assert rows["2331"] is core


def test_overlap_requires_explicit_flag() -> None:
    alt = _alt_row(overlap=False)
    with pytest.raises(ReviewSetResolutionError, match="overlap flag"):
        resolve_review_set_rows(
            {
                "review_tickers": ["2331"],
                "longlist": [_core_row()],
                "longlist_alt": {"entries": [alt]},
            }
        )


def test_overlap_rejects_conflicting_common_fact() -> None:
    alt = _alt_row(overlap=True)
    alt["market_price_yen"] = 999.0
    with pytest.raises(ReviewSetResolutionError, match="market_price_yen"):
        resolve_review_set_rows(
            {
                "review_tickers": ["2331"],
                "longlist": [_core_row()],
                "longlist_alt": {"entries": [alt]},
            }
        )


def test_alt_only_source_must_not_claim_overlap() -> None:
    with pytest.raises(ReviewSetResolutionError, match="no Core source row"):
        resolve_review_set_rows(
            {
                "review_tickers": ["2331"],
                "longlist": [],
                "longlist_alt": {"entries": [_alt_row(overlap=True)]},
            }
        )


def test_duplicate_source_row_fails_closed() -> None:
    row = _core_row()
    with pytest.raises(ReviewSetResolutionError, match="duplicate ticker 2331"):
        resolve_review_set_rows({"review_tickers": ["2331"], "longlist": [row, deepcopy(row)]})


def test_review_ticker_without_source_row_fails_closed() -> None:
    with pytest.raises(ReviewSetResolutionError, match="no Review Set source row"):
        resolve_review_set_rows({"review_tickers": ["2331"], "longlist": []})
