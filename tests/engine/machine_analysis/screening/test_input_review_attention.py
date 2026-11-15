"""予備laneの有限注意順。代表抽出や投資優位をtestしない。"""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest
from tests.engine.machine_analysis.screening.test_technical_financial import row
from tests.tools.test_technical_discovery import _metadata

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies import technical_financial as owner
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
    discovery_display_order,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    evaluate_technical_financial_rows,
    technical_financial_selection_id,
)


def review(ticker, routes=("a",), *, share_gap=False):
    original = row(ticker, routes, state="input_unresolved")
    if not share_gap:
        return original
    return replace(
        original,
        screening_lane="input_review",
        financial_gate={
            **original.financial_gate,
            "state": "not_selected",
            "checks": {"net_assets": {"state": "not_met", "value": -10}},
        },
        financial_paths={
            **original.financial_paths,
            "input_review_reasons": [
                {"path_id": "earning_power", "reason": "missing_capital_basis"},
                {"path_id": "business_expansion", "reason": "missing_capital_basis"},
            ],
        },
    )


def result(rows, *, method="technical-financial-discovery-v9", order=None):
    return TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method=method),
        "T",
        "G",
        tuple(rows),
        discovery_display_order(rows) if order is None else order,
        {},
    )


def test_input_review_is_lane_local_and_other_lanes_keep_their_full_order():
    # The third company only has explicit financial-input questions, not a T pass.
    reviews = (review("2001", ("a",)), review("2002", ("b",)), review("2003", (), share_gap=True))
    growth = tuple(row(str(1000 + i), ("a",)) for i in range(50))
    alternative = replace(
        row("3001", ("b",), state="not_selected"),
        financial_paths={"matched_routes": ["earning_power"], "paths": []},
        screening_lane="alternative_financial_research",
    )
    rows = (*growth, *reviews, alternative)
    order = discovery_display_order(rows)
    review_tickers = {r.ticker for r in reviews}
    assert tuple(t for t in order if t in review_tickers) == discovery_display_order(reviews)
    # The old global route walk remains the owner for non-review lanes and their slots.
    previous = owner._route_display_order(rows)
    assert [t for t in order if t not in review_tickers] == [
        t for t in previous if t not in review_tickers
    ]
    assert [i for i, t in enumerate(order) if t not in review_tickers] == [
        i for i, t in enumerate(previous) if t not in review_tickers
    ]
    assert set(order) == {r.ticker for r in rows}


def test_one_company_hash_is_stable_and_does_not_rank_by_values():
    rows = tuple(review(t) for t in ("1001", "1002", "1003", "100A", "2001", "3001"))
    expected = tuple(
        sorted(
            (r.ticker for r in rows),
            key=lambda t: (sha256(f"baibai-loop/attention-control/v1\0{t}".encode()).digest(), t),
        )
    )
    assert expected != tuple(sorted(expected))
    assert discovery_display_order(rows) == expected
    assert discovery_display_order(tuple(reversed(rows))) == expected
    changed = tuple(
        replace(r, financial_gate={**r.financial_gate, "checks": {"profit": i * 1000000}})
        for i, r in enumerate(rows)
    )
    assert discovery_display_order(changed) == expected


def test_hash_collision_has_a_deterministic_ticker_tie_break(monkeypatch):
    class Collision:
        def digest(self):
            return b"same"

    monkeypatch.setattr(owner, "sha256", lambda _: Collision())
    assert discovery_display_order((review("2002"), review("100A"), review("1001"))) == (
        "1001",
        "100A",
        "2002",
    )


def test_multiple_routes_share_one_company_key_and_keep_all_review_questions(monkeypatch):
    rows = (review("1001", ("a", "b")), review("1002", (), share_gap=True), review("1003"))
    before = deepcopy(rows)
    original = owner.sha256
    keys = []

    def observe(value):
        keys.append(value)
        return original(value)

    monkeypatch.setattr(owner, "sha256", observe)
    order = discovery_display_order(rows)
    assert sorted(keys) == [f"baibai-loop/attention-control/v1\0{r.ticker}".encode() for r in rows]
    assert len(order) == len(set(order)) == 3
    assert rows == before
    brief = technical_financial_screening_brief(result(rows), lane="input_review")
    gap = next(c for c in brief["cards"] if c["ticker"] == "1002")
    assert gap["financial_gate"]["state"] == "not_selected"
    assert gap["financial_gate"]["checks"]["net_assets"]["value"] == -10
    assert gap["financial_paths"]["matched_routes"] == []
    assert gap["financial_paths"]["coordinates"] == []
    assert len(gap["financial_paths"]["input_review_reasons"]) == 2


@pytest.mark.parametrize("size", [0, 1, 36, 37, 73])
def test_all_pages_are_unique_repeatable_and_keep_unmatched_inputs(size):
    rows = tuple(
        review(str(1000 + i), () if i % 2 else ("a",), share_gap=bool(i % 2)) for i in range(size)
    )
    saved = result(rows)
    pages = [
        technical_financial_screening_brief(saved, lane="input_review", page=p)
        for p in range((size + 35) // 36 + 1)
    ]
    tickers = tuple(c["ticker"] for page in pages for c in page["cards"])
    assert tickers == saved.lane_tickers("input_review")
    assert len(tickers) == len(set(tickers)) == size
    assert pages[-1]["cards"] == []
    assert pages[-1]["page"]["next"] is None
    for page in pages:
        assert page == technical_financial_screening_brief(
            saved, lane="input_review", page=page["page"]["index"]
        )


def test_brief_explains_only_the_new_input_lane_and_counts_companies_not_routes():
    rows = (review("1001"), review("1002", (), share_gap=True), row("3001"))
    saved = result(rows)
    brief = technical_financial_screening_brief(saved, lane="input_review")
    assert (
        brief["presentation"]["kind"] == "saved_lane_local_route_round_robin_not_investment_ranking"
    )
    assert brief["presentation"]["ties"] == "stable_company_hash_then_ticker_not_quality_or_return"
    assert brief["lanes"]["input_review"]["financial_gate_states"] == {
        "input_unresolved": 1,
        "not_selected": 1,
    }
    for lane in ("growth_research", "alternative_financial_research"):
        other = technical_financial_screening_brief(saved, lane=lane)
        assert other["presentation"]["ties"] == "ticker_code_order_not_quality_or_return"
    detail = technical_financial_screening_brief(saved, page=None)
    assert detail["cards"] == []
    assert detail["presentation"]["ties"] == (
        "input_review_company_hash_other_lanes_ticker_code_not_quality_or_return"
    )


@pytest.mark.parametrize(
    "method",
    [
        "technical-financial-discovery-v1",
        "technical-financial-discovery-v2",
        "technical-financial-discovery-v3",
        "technical-financial-discovery-v4",
        "technical-financial-discovery-v5",
        "technical-financial-discovery-v6",
        "technical-financial-discovery-v7",
        "technical-financial-discovery-v8",
    ],
)
def test_historical_policy_keeps_stored_order_description_and_cannot_regenerate(method):
    rows = (review("1001"), review("1002"), review("1003"))
    stored_order = ("1003", "1001", "1002")
    saved = result(rows, method=method, order=stored_order)
    brief = technical_financial_screening_brief(saved, lane="input_review")
    assert tuple(c["ticker"] for c in brief["cards"]) == stored_order
    if method in {
        "technical-financial-discovery-v3",
        "technical-financial-discovery-v4",
        "technical-financial-discovery-v5",
        "technical-financial-discovery-v6",
        "technical-financial-discovery-v7",
        "technical-financial-discovery-v8",
    }:
        assert (
            brief["presentation"]["ties"] == "stable_company_hash_then_ticker_not_quality_or_return"
        )
        assert "financial_gate_states" in brief["lanes"]["input_review"]
    else:
        assert brief["presentation"]["ties"] == "ticker_code_order_not_quality_or_return"
        assert "financial_gate_states" not in brief["lanes"]["input_review"]
    with pytest.raises(ValueError, match=r"historical.*read-only"):
        evaluate_technical_financial_rows(_metadata(), tickers=(), rows={}, policy=saved.policy)
    assert technical_financial_selection_id(
        "fixed", policy=saved.policy
    ) != technical_financial_selection_id("fixed", policy=TechnicalFinancialPolicy())


def test_input_lane_does_not_promote_rows_without_a_hypothesis():
    outside = replace(review("9999"), screening_lane="no_current_hypothesis")
    assert discovery_display_order((outside, review("1001"))) == ("1001",)
