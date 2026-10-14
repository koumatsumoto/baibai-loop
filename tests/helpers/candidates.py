"""保存済みL2候補projection helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

RULES_HASH = "a" * 64
METHOD_HASH = "b" * 64


def candidate_method() -> dict[str, Any]:
    return {
        "method_id": "candidate-discovery-v1",
        "method_hash": METHOD_HASH,
        "nomination_depth": 20,
    }


def review_set_analysis(*, er_annual: float = 0.1) -> dict[str, Any]:
    return {
        "identity_liquidity": {
            "market_cap_oku": 500.0,
            "avg_turnover_oku": 5.0,
            "listing_span_days": 1000.0,
            "jpx_flags": [],
        },
        "valuation": {
            "per_forward": 10.0,
            "per_trailing": 11.0,
            "pbr": 0.8,
            "ev_ebitda": 5.0,
            "p_s": 1.0,
            "pcfr": 8.0,
        },
        "current_earnings": {
            "fcf_yield": 0.08,
            "ocf_yield": 0.1,
            "forecast_special_gain_flag": False,
            "forecast_full_year_loss_flag": False,
        },
        "normalized_earnings": {
            "normalized_per_3fy": 10.0,
            "normalized_per_3fy_sector_gap": -0.2,
        },
        "asset_value": {
            "asset_backed_ratio": 0.5,
            "net_cash_to_market_cap": 0.25,
            "investment_securities": 10.0,
            "equity_ratio": 0.6,
        },
        "reinvestment": {
            "p_s_sector_gap": -0.4,
            "operating_return_on_capital_proxy": 0.12,
            "sales_yoy": 0.05,
            "operating_margin": 0.12,
            "fcf_yield": 0.08,
        },
        "expected_return": {
            "er_annual": er_annual,
            "er_reversion_annual": er_annual,
            "er_carry_annual": 0.0,
            "fv_sector_median_yen": 1200.0,
            "fv_self_range_yen": 1100.0,
            "er_origin": "estimate",
            "er_model_version": "expected-return-v1",
            "er_unit": "annual_ratio",
            "er_assumptions": "test assumptions",
        },
        "data_quality": {
            "bs_carry_forward_fields": None,
            "bs_carry_forward_lag_days": None,
            "edinet_failure_reasons": None,
            "stale_fin_flag": False,
        },
        "context": {
            "next_earnings_status": None,
            "next_earnings_estimated_date": None,
            "margin_short_to_adv": None,
            "tse_capital_policy_status": None,
            "large_holding_event_recent": False,
            "tender_offer_event_recent": False,
        },
    }


def candidate_snapshot(ticker: str, *, position: int, er_annual: float = 0.1) -> dict[str, Any]:
    return {
        "name": f"Company {ticker}",
        "sector_33": "情報・通信業",
        "nominations": [
            {
                "valuation_approach_id": "current-earnings-power",
                "valuation_method_id": "current-earnings-power-v1",
                "rank": position,
            }
        ],
        "analysis": review_set_analysis(er_annual=er_annual),
    }


def published_review_set(
    *,
    as_of: str = "2026-07-19",
    review_set_id: str = "review-set-test",
    run_revision_id: str = "runrev-test",
    tickers: Sequence[str] = ("2331",),
) -> dict[str, Any]:
    entries = []
    for position, ticker in enumerate(sorted(tickers), start=1):
        snapshot = candidate_snapshot(ticker, position=position)
        entries.append(
            {
                "ticker": ticker,
                "name": snapshot["name"],
                "sector_33": snapshot["sector_33"],
                "nominations": snapshot["nominations"],
                "analysis": snapshot["analysis"],
            }
        )
    return {
        "schema_version": 2,
        "kind": "review_set",
        "review_set_id": review_set_id,
        "run_revision_id": run_revision_id,
        "as_of": as_of,
        "created_at": f"{as_of}T13:00:00+09:00",
        "screening_rules_hash": RULES_HASH,
        "method": candidate_method(),
        "entries": entries,
        "diagnostics": {},
    }
