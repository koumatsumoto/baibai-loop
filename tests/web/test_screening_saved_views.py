from __future__ import annotations

from datetime import date, datetime

import pytest

from baibai_web.readmodel.stocks import build_screening
from baibai_web.readmodel.stocks.screening import (
    _stock_analysis_row_view,
)
from baibai_web.sources.db_sources import DbScreeningSource
from baibai_web.sources.types import ScreeningRunRecord


def _run() -> ScreeningRunRecord:
    return ScreeningRunRecord(
        run_id="screening-20260828",
        as_of=date(2026, 8, 28),
        generated_at=datetime.fromisoformat("2026-08-28T18:00:00+09:00"),
        universe_size=1,
        run_revision_id="run-current",
        rules_ref=None,
        screening_rules_hash="rules-current",
        er_model_version="expected-return-v1",
        candidate_discovery={
            "method": {"method_id": "synthetic", "method_hash": "a" * 64, "nomination_depth": 20},
            "diagnostics": {
                "common_eligible_count": 1,
                "candidate_count": 1,
                "candidate_counts_by_approach": {
                    "current-earnings-power": 1,
                    "normalized-earnings-power": 0,
                    "asset-value": 0,
                    "reinvestment-value": 0,
                },
            },
        },
        rows=(_frozen_entry(),),
    )


def _frozen_entry() -> dict[str, object]:
    return {
        "ticker": "2331",
        "name": "Frozen name",
        "sector_33": "情報・通信業",
        "market_cap_oku": 123.0,
        "avg_turnover_oku": 4.5,
        "price_history_span_days": 900,
        "jpx_flags": ["prime"],
        "per_forward": 8.0,
        "per_trailing": 9.0,
        "pbr": 0.8,
        "ev_ebitda": 4.0,
        "p_s": 0.7,
        "pcfr": 5.0,
        "candidate": {
            "common_eligible": True,
            "normalized_per_3fy_sector_gap": -2.0,
            "reasons": [
                {
                    "valuation_approach_id": "current-earnings-power",
                    "valuation_method_id": "current-earnings-v1",
                    "rank": 1,
                }
            ],
        },
        "metrics": {
            "fcf_yield": 0.08,
            "ocf_yield": 0.09,
            "forecast_special_gain_flag": False,
            "forecast_full_year_loss_flag": False,
            "normalized_per_3fy": 10.0,
            "net_cash_to_market_cap": 0.3,
            "asset_backed_ratio": 0.4,
            "investment_securities": 100.0,
            "equity_ratio": 0.6,
            "p_s_sector_gap": -0.4,
            "operating_profit_ttm": 18.0,
            "sales_ttm": 100.0,
            "same_state_equity_yen": 100.0,
            "debt": 40.0,
            "cash": 20.0,
            "sales_yoy": 0.12,
            "er_annual": 0.11,
            "er_reversion_annual": 0.07,
            "er_carry_annual": 0.04,
            "fv_sector_median_yen": 1500.0,
            "fv_self_range_yen": 1400.0,
            "er_origin": "sector",
            "er_model_version": "expected-return-v1",
            "er_unit": "fraction",
            "er_assumptions": "frozen",
            "bs_carry_forward_fields": "cash",
            "bs_carry_forward_lag_days": 30,
            "stale_fin_flag": True,
            "next_earnings_status": "estimated",
            "next_earnings_estimated_date": "2026-09-01",
            "margin_short_to_adv": 0.2,
            "tse_capital_policy_status": "published",
            "large_holding_event_recent": False,
            "tender_offer_event_recent": False,
        },
    }


def _source(tmp_path, mocker) -> DbScreeningSource:
    source = DbScreeningSource(tmp_path / "runs.sqlite")
    mocker.patch.object(source, "latest_run", return_value=_run())
    return source


def _dependencies(mocker):
    ledger = mocker.Mock()
    ledger.exists.return_value = False
    research = mocker.Mock()
    research.revisions.return_value = []
    research.proposals.return_value = []
    return ledger, research


@pytest.mark.parametrize("sales_yoy", [0.12, -0.01, None])
def test_stock_analysis_has_candidate_and_all_saved_display_values(tmp_path, mocker, sales_yoy):
    source = _source(tmp_path, mocker)
    source.latest_run().rows[0]["metrics"]["sales_yoy"] = sales_yoy
    ledger, research = _dependencies(mocker)
    view = build_screening(source, ledger, research)
    entry = view.stock_analyses[0]
    assert entry.name == "Frozen name"
    assert entry.candidate.reasons[0].rank == 1
    assert entry.per_forward == 8.0
    assert entry.normalized_per_3fy == 10.0
    assert entry.net_cash_to_market_cap == 0.3
    assert entry.operating_margin == 0.18
    assert entry.operating_return_on_capital_proxy == 0.15
    assert entry.sales_yoy == sales_yoy
    assert entry.er_annual == 0.11
    assert entry.stale_fin_flag is True
    assert entry.next_earnings_status == "estimated"


def test_date_only_earnings_status_preserves_saved_raw_annotation(tmp_path, mocker):
    source = _source(tmp_path, mocker)
    raw = source.latest_run().rows[0]["metrics"]
    raw["next_earnings_status"] = "announced"
    view = build_screening(source, *_dependencies(mocker))
    assert view.stock_analyses[0].next_earnings_status == "scheduled_date_reached"
    assert raw["next_earnings_status"] == "announced"


def test_security_row_projects_saved_machine_anchor_and_quality_statuses() -> None:
    row = {
        "candidate": {
            "common_eligible": False,
            "reasons": [],
            "normalized_per_3fy_sector_gap": None,
        },
        "ticker": "2331",
        "name": "Current name",
        "metrics": {
            "market_price_yen": 1000.0,
            "fv_sector_median_yen": 1500.0,
            "stale_fin_flag": True,
            "dividend_basis": "unresolved_split_basis",
        },
    }

    view = _stock_analysis_row_view(
        row,
        held=set(),
        reserved=set(),
        researched=set(),
    )

    assert view.fair_value_anchor_yen == 1500.0
    assert view.fair_value_gap_pct == 50.0
    assert view.data_quality_flags == ["予定日到来・財務未照合", "unresolved_split_basis"]

    without_price = _stock_analysis_row_view(
        {
            "ticker": "2331",
            "candidate": {
                "common_eligible": False,
                "reasons": [],
                "normalized_per_3fy_sector_gap": None,
            },
            "metrics": {"dividend_basis": "forecast_annual"},
        },
        held=set(),
        reserved=set(),
        researched=set(),
    )
    assert without_price.fair_value_gap_pct is None
    assert without_price.data_quality_flags == []
