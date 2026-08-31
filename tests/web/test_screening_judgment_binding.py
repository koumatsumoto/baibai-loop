from __future__ import annotations

from datetime import date, datetime

from tests.helpers.research_triage import research_triage_payload, skip_entry

from baibai_web.readmodel.builders import _candidate_row_view, build_screening
from baibai_web.readmodel.models import ReviewSetEntryView
from baibai_web.sources.db_sources import DbCandidatesSource
from baibai_web.sources.types import CandidatesRun


def _run() -> CandidatesRun:
    return CandidatesRun(
        run_id="screening-20260828",
        run_date=date(2026, 8, 28),
        asof_date=date(2026, 8, 28),
        run_at=datetime.fromisoformat("2026-08-28T18:00:00+09:00"),
        universe_size=0,
        run_revision_id="run-current",
        rules_ref=None,
        screening_rules_hash="rules-current",
        er_model_version="expected-return-v1",
        rows=(),
    )


def _review_set() -> dict[str, object]:
    return {
        "review_set_id": "review-set-current",
        "run_revision_id": "run-current",
        "created_at": "2026-08-28T18:01:00+09:00",
        "payload": {"entries": []},
    }


def _frozen_entry() -> dict[str, object]:
    return {
        "review_position": 1,
        "ticker": "2331",
        "name": "Frozen name",
        "sector_33": "情報・通信業",
        "nominations": [
            {
                "valuation_approach_id": "current-earnings-power",
                "valuation_method_id": "current-earnings-v1",
                "rank": 2,
            }
        ],
        "support_count": 1,
        "rank_vector": [2, 99, 99, 99],
        "analysis": {
            "identity_liquidity": {
                "market_cap_oku": 123.0,
                "avg_turnover_oku": 4.5,
                "listing_span_days": 900,
                "jpx_flags": ["prime"],
            },
            "valuation": {
                "per_forward": 8.0,
                "per_trailing": 9.0,
                "pbr": 0.8,
                "ev_ebitda": 4.0,
                "p_s": 0.7,
                "pcfr": 5.0,
            },
            "current_earnings": {
                "fcf_yield": 0.08,
                "ocf_yield": 0.09,
                "forecast_special_gain_flag": False,
                "forecast_full_year_loss_flag": False,
            },
            "normalized_earnings": {
                "normalized_per_3fy": 10.0,
                "normalized_per_3fy_sector_gap": -2.0,
            },
            "asset_value": {
                "asset_backed_ratio": 0.4,
                "net_cash_to_market_cap": 0.3,
                "investment_securities": 100.0,
                "equity_ratio": 0.6,
            },
            "reinvestment": {
                "p_s_sector_gap": -0.4,
                "operating_return_on_capital_proxy": 0.15,
                "sales_yoy": 0.12,
                "operating_margin": 0.18,
                "fcf_yield": 0.08,
            },
            "expected_return": {
                "er_annual": 0.11,
                "er_reversion_annual": 0.07,
                "er_carry_annual": 0.04,
                "fv_sector_median_yen": 1500.0,
                "fv_self_range_yen": 1400.0,
                "er_origin": "sector",
                "er_model_version": "expected-return-v1",
                "er_unit": "fraction",
                "er_assumptions": "frozen",
            },
            "data_quality": {
                "bs_carry_forward_fields": "cash",
                "bs_carry_forward_lag_days": 30,
                "edinet_failure_reasons": None,
                "stale_fin_flag": True,
            },
            "context": {
                "next_earnings_status": "estimated",
                "next_earnings_estimated_date": "2026-09-01",
                "margin_short_to_adv": 0.2,
                "tse_capital_policy_status": "published",
                "large_holding_event_recent": False,
                "tender_offer_event_recent": False,
            },
        },
    }


def _assessment(assessment_id: str, research_triage_id: str) -> dict[str, object]:
    return {
        "capital_allocation_assessment_id": assessment_id,
        "as_of": "2026-08-28",
        "published_at": "2026-08-28T18:03:00+09:00",
        "result": "no_allocation",
        "headline": "要求利回り未達のため配分しない",
        "research_triage_id": research_triage_id,
        "alternatives": [],
    }


def _source(tmp_path, mocker) -> DbCandidatesSource:
    source = DbCandidatesSource(tmp_path / "runs.sqlite", tmp_path / "app.sqlite")
    mocker.patch.object(source, "latest_run", return_value=_run())
    mocker.patch.object(source, "review_sets", return_value=[_review_set()])
    return source


def _dependencies(mocker):
    ledger = mocker.Mock()
    ledger.exists.return_value = False
    research = mocker.Mock()
    research.revisions.return_value = []
    return ledger, research


def test_screening_omits_judgments_from_another_review_set_cycle(tmp_path, mocker) -> None:
    source = _source(tmp_path, mocker)
    old_triage = research_triage_payload(
        research_triage_id="triage-old",
        review_set_id="review-set-old",
        run_revision_id="run-old",
        entries=[skip_entry("3836")],
    )
    mocker.patch.object(source, "research_triages", return_value=[old_triage])
    mocker.patch.object(
        source, "assessments", return_value=[_assessment("assessment-old", "triage-old")]
    )
    ledger, research = _dependencies(mocker)

    view = build_screening(source, ledger, research)

    assert [item.review_set_id for item in view.review_sets] == ["review-set-current"]
    assert view.research_triages == []
    assert view.capital_allocation_assessments == []


def test_screening_projects_only_the_assessment_bound_to_the_current_triage(
    tmp_path, mocker
) -> None:
    source = _source(tmp_path, mocker)
    current_triage = research_triage_payload(
        research_triage_id="triage-current",
        review_set_id="review-set-current",
        run_revision_id="run-current",
        entries=[skip_entry("6419")],
    )
    mocker.patch.object(source, "research_triages", return_value=[current_triage])
    mocker.patch.object(
        source,
        "assessments",
        return_value=[
            _assessment("assessment-old", "triage-old"),
            _assessment("assessment-current", "triage-current"),
        ],
    )
    ledger, research = _dependencies(mocker)

    view = build_screening(source, ledger, research)

    assert [item.research_triage_id for item in view.research_triages] == ["triage-current"]
    assert [
        item.capital_allocation_assessment_id for item in view.capital_allocation_assessments
    ] == ["assessment-current"]


def test_review_set_projects_the_frozen_typed_analysis_without_current_row_join(
    tmp_path, mocker
) -> None:
    source = _source(tmp_path, mocker)
    frozen = _review_set()
    frozen["payload"] = {"entries": [_frozen_entry()]}
    mocker.patch.object(source, "review_sets", return_value=[frozen])
    mocker.patch.object(source, "research_triages", return_value=[])
    mocker.patch.object(source, "assessments", return_value=[])
    ledger, research = _dependencies(mocker)

    view = build_screening(source, ledger, research)

    entry = view.review_sets[0].entries[0]
    assert entry.name == "Frozen name"
    assert entry.nominations[0].rank == 2
    assert entry.analysis.valuation.per_forward == 8.0
    assert entry.analysis.normalized_earnings.normalized_per_3fy == 10.0
    assert entry.analysis.asset_value.net_cash_to_market_cap == 0.3
    assert entry.analysis.reinvestment is not None
    assert entry.analysis.reinvestment.sales_yoy == 0.12
    assert entry.analysis.expected_return.er_annual == 0.11
    assert entry.analysis.data_quality.stale_fin_flag is True
    assert entry.analysis.context.next_earnings_status == "estimated"


def test_security_row_projects_frozen_fv_gap_and_quality_statuses() -> None:
    frozen = ReviewSetEntryView.model_validate(_frozen_entry())
    row = {
        "ticker": "2331",
        "name": "Current name",
        "metrics": {
            "market_price_yen": 1000.0,
            "stale_fin_flag": True,
            "dividend_basis": "unresolved_split_basis",
        },
    }

    view = _candidate_row_view(
        row,
        held=set(),
        reserved=set(),
        researched=set(),
        fair_value={"2331": frozen},
    )

    assert view.fair_value_anchor_yen == 1500.0
    assert view.fair_value_gap_pct == 50.0
    assert view.data_quality_flags == ["stale_fin", "unresolved_split_basis"]

    without_price = _candidate_row_view(
        {"ticker": "2331", "metrics": {"dividend_basis": "forecast_annual"}},
        held=set(),
        reserved=set(),
        researched=set(),
        fair_value={"2331": frozen},
    )
    assert without_price.fair_value_gap_pct is None
    assert without_price.data_quality_flags == []
