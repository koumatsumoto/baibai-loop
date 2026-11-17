"""候補所属を変えず、コード番号に依存しない有限の閲覧順を保存する。"""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_input_review_attention import result
from tests.engine.machine_analysis.screening.test_technical_financial import row
from tests.helpers.technical_discovery import _metadata

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies import technical_financial as owner
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    RESEARCH_LANES,
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
    discovery_display_order,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    evaluate_technical_financial_rows,
    technical_financial_selection_id,
)


def lane_row(ticker, lane, routes=("a",)):
    original = row(ticker, routes)
    return replace(
        original,
        screening_lane=lane,
        financial_gate={
            **original.financial_gate,
            "state": {
                "growth_research": "research_candidate",
                "input_review": "input_unresolved",
                "alternative_financial_research": "not_selected",
                "no_current_hypothesis": "not_selected",
            }[lane],
            "checks": {"known_adverse": {"state": "not_met", "value": -10}},
        },
    )


@pytest.mark.parametrize("lane", RESEARCH_LANES)
def test_each_lane_uses_one_stable_company_order_not_ticker_size(lane):
    rows = tuple(lane_row(t, lane) for t in ("1001", "1002", "100A", "2001", "3001", "9001"))
    expected = tuple(
        sorted(
            (r.ticker for r in rows),
            key=lambda t: (sha256(f"baibai-loop/attention-control/v1\0{t}".encode()).digest(), t),
        )
    )
    assert expected != tuple(sorted(expected))
    assert discovery_display_order(rows) == expected
    assert discovery_display_order(tuple(reversed(rows))) == expected


def test_all_lane_slots_membership_and_existing_review_order_are_preserved():
    rows = (
        *(
            lane_row(f"{1000 + 100 * i + j}", lane, ("a", "b") if j % 2 else ("a",))
            for i, lane in enumerate(RESEARCH_LANES)
            for j in range(7)
        ),
        lane_row("9999", "no_current_hypothesis"),
    )
    before = deepcopy(rows)
    original_walk = owner._route_display_order(rows)
    lanes = {r.ticker: r.screening_lane for r in rows}
    current = discovery_display_order(rows)
    assert [lanes[t] for t in current] == [lanes[t] for t in original_walk]
    assert set(current) == set(original_walk)
    assert len(current) == len(set(current)) == len(rows) - 1
    for lane in RESEARCH_LANES:
        local_rows = tuple(r for r in rows if r.screening_lane == lane)
        assert tuple(t for t in current if lanes[t] == lane) == owner._route_display_order(
            local_rows, company_hash=True
        )
    assert rows == before


def test_multiple_routes_use_one_company_key_without_promoting_known_failure(monkeypatch):
    rows = tuple(
        lane_row(f"{1000 + 100 * i + j}", lane, ("a", "b"))
        for i, lane in enumerate(RESEARCH_LANES)
        for j in range(3)
    )
    before = deepcopy(rows)
    original = owner.sha256
    keys = []

    def observe(value):
        keys.append(value)
        return original(value)

    monkeypatch.setattr(owner, "sha256", observe)
    ordered = discovery_display_order(rows)
    assert sorted(keys) == [f"baibai-loop/attention-control/v1\0{r.ticker}".encode() for r in rows]
    assert len(ordered) == len(set(ordered)) == len(rows)
    assert rows == before
    assert all(r.financial_gate["checks"]["known_adverse"]["value"] == -10 for r in rows)


@pytest.mark.parametrize("lane", RESEARCH_LANES)
def test_numerical_support_or_adverse_values_do_not_reorder_a_lane(lane):
    rows = tuple(lane_row(f"{1000 + i}", lane) for i in range(12))
    changed = tuple(
        replace(r, financial_gate={**r.financial_gate, "checks": {"profit": i * 1000000}})
        for i, r in enumerate(rows)
    )
    assert discovery_display_order(changed) == discovery_display_order(rows)


@pytest.mark.parametrize("lane", RESEARCH_LANES)
def test_v12_pages_are_complete_repeatable_and_do_not_claim_investment_priority(lane):
    rows = tuple(lane_row(f"{1000 + i}", lane) for i in range(73))
    saved = result(rows, method="technical-financial-discovery-v12")
    pages = [technical_financial_screening_brief(saved, lane=lane, page=p) for p in range(4)]
    assert tuple(c["ticker"] for page in pages for c in page["cards"]) == saved.lane_tickers(lane)
    assert pages[-1]["cards"] == []
    for page in pages:
        assert (
            page["presentation"]["ties"] == "stable_company_hash_then_ticker_not_quality_or_return"
        )
        assert (
            page["presentation"]["kind"]
            == "saved_lane_local_route_round_robin_not_investment_ranking"
        )
        assert page == technical_financial_screening_brief(
            saved, lane=lane, page=page["page"]["index"]
        )
    detail = technical_financial_screening_brief(saved, page=None)
    assert detail["presentation"]["ties"] == "stable_company_hash_then_ticker_not_quality_or_return"


@pytest.mark.parametrize("method", ["v9", "v10", "v11"])
def test_recent_saved_policy_keeps_exact_order_bytes_and_is_read_only(method):
    rows = tuple(lane_row(t, "growth_research") for t in ("1001", "1002", "1003"))
    stored_order = ("1003", "1001", "1002")
    saved = result(rows, method=f"technical-financial-discovery-{method}", order=stored_order)
    codec = TypeAdapter(TechnicalFinancialSelection)
    payload = codec.dump_json(saved)
    restored = codec.validate_json(payload)
    assert codec.dump_json(restored) == payload
    brief = technical_financial_screening_brief(restored)
    assert tuple(c["ticker"] for c in brief["cards"]) == stored_order
    assert brief["presentation"]["ties"] == "ticker_code_order_not_quality_or_return"
    with pytest.raises(ValueError, match=r"historical.*read-only"):
        evaluate_technical_financial_rows(_metadata(), tickers=(), rows={}, policy=saved.policy)
    assert technical_financial_selection_id("fixed", policy=saved.policy) != (
        technical_financial_selection_id("fixed", policy=TechnicalFinancialPolicy())
    )


def test_empty_and_no_hypothesis_rows_remain_outside():
    assert discovery_display_order(()) == ()
    assert discovery_display_order((lane_row("9999", "no_current_hypothesis"),)) == ()
