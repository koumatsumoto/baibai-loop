"""新しい候補政策の接続・全分母・表示容量。投資優位はtestしない。"""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter, ValidationError
from tests.tools.test_technical_financial_candidates import _seed

from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies import technical_financial_selection as owner
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialRow,
    discovery_display_order,
    financial_screening_lane,
)


def row(ticker, routes=("a",), state="research_candidate"):
    return TechnicalFinancialRow(
        ticker,
        ticker,
        "業種",
        {
            "state": "matched" if routes else "not_matched",
            "matched_routes": list(routes),
            "routes": {},
            "price_context": {"ready": True},
        },
        {"state": state, "matched_routes": [], "routes": {}, "checks": {}},
        {"matched_routes": [], "paths": []},
        financial_screening_lane(
            growth_state=state, technical_match=bool(routes), financial_path_match=False
        ),
    )


def evaluate_fixed(fixed, *, page_size=2):
    metadata = GenerationMetadata.model_validate(fixed.model_dump(exclude={"models"}))
    tickers = tuple(t for t, plan in metadata.subject_plan.items() if plan.scope == "stock")
    rows = {
        ticker: owner.project_technical_financial_row(
            ticker,
            models=tuple(m for m in fixed.models if m.subject == ticker),
            metadata=metadata,
        )
        for ticker in tickers
    }
    return owner.evaluate_technical_financial_rows(
        metadata, tickers=tickers, rows=rows, policy=TechnicalFinancialPolicy(page_size=page_size)
    )


def test_round_robin_has_no_economic_rank_vote_or_duplicate():
    rows = (
        row("1001", ("a", "b")),
        row("1002", ("a",)),
        row("1003", ("a",)),
        row("2001", ("b",)),
        row("3001", ("c",)),
    )
    expected = ("1001", "2001", "3001", "1002", "1003")
    assert discovery_display_order(rows) == expected
    assert discovery_display_order(tuple(reversed(rows))) == expected
    assert len(set(expected)) == len(expected)


def test_unresolved_is_visible_but_nonmatch_and_unevaluated_are_not_promoted():
    rows = (
        row("1001"),
        row("1002", state="input_unresolved"),
        row("1003", state="not_selected"),
        row("1004", (), "not_evaluated"),
    )
    assert discovery_display_order(rows) == ("1001", "1002")
    assert rows[1].financial_gate["state"] == "input_unresolved"


@pytest.mark.parametrize("size", [0, -1, 37, True])
def test_page_size_is_bounded_not_a_research_budget(size):
    with pytest.raises(ValidationError):
        TechnicalFinancialPolicy(page_size=size)


def test_gate_runs_for_every_stock_once_and_preserves_missing_known_failure(tmp_path, monkeypatch):
    fixed = _seed(tmp_path / "runs")
    real = owner.evaluate_financial_growth_gate
    calls = []

    def gate(ticker, **kwargs):
        calls.append(ticker)
        assert {model.model_id for model in kwargs["models"]} <= {
            "company_growth",
            "company_financials",
        }
        assert kwargs["metadata"].generation_id == fixed.generation_id
        return real(ticker, **kwargs)

    monkeypatch.setattr(owner, "evaluate_financial_growth_gate", gate)
    selected = evaluate_fixed(fixed)
    assert calls == ["1234", "2345", "3456", "4567", "5678", "6789"]
    rows = {row.ticker: row for row in selected.result.rows}
    assert selected.universe_tickers == tuple(rows)
    assert selected.result.tickers == ("1234", "3456", "4567", "5678", "6789")
    assert rows["2345"].financial_gate["checks"]["positive_net_assets"]["value"] == -10
    assert rows["2345"].financial_gate["state"] == "not_selected"
    assert rows["3456"].financial_gate["state"] == "input_unresolved"
    assert rows["3456"].financial_gate["missing_models"] == ["company_growth"]
    assert rows["4567"].financial_gate["state"] == "research_candidate"
    assert rows["5678"].financial_gate["state"] == "research_candidate"
    assert rows["5678"].technical["state"] == "unavailable"
    assert rows["5678"].technical["observation_failures"]
    assert selected.result.summary["financial_gate"] == {
        "research_candidate": 3,
        "not_selected": 1,
        "input_unresolved": 2,
        "not_evaluated": 0,
    }
    assert len(selected.result.display_order) > selected.result.policy.page_size
    for item in rows.values():
        assert "observations" not in item.financial_gate
        assert "adoption_status" not in item.financial_gate
    codec = TypeAdapter(ScreeningSelection)
    assert codec.validate_json(codec.dump_json(selected)) == selected


def test_complete_plan_preserves_whole_missing_and_empty_universe(tmp_path):
    fixed = _seed(tmp_path / "runs")
    metadata = GenerationMetadata.model_validate(fixed.model_dump(exclude={"models"}))
    tickers = tuple(t for t, plan in metadata.subject_plan.items() if plan.scope == "stock")
    selected = owner.evaluate_technical_financial_rows(
        metadata, tickers=tickers, rows={}, policy=TechnicalFinancialPolicy()
    )
    assert len(selected.result.rows) == len(tickers)
    assert selected.result.summary["technical"]["unavailable"] == len(tickers)
    assert selected.result.summary["financial_gate"]["input_unresolved"] == len(tickers)
    empty = owner.evaluate_technical_financial_rows(
        metadata.model_copy(update={"subject_plan": {}}),
        tickers=(),
        rows={},
        policy=TechnicalFinancialPolicy(),
    )
    assert empty.result.rows == ()
    assert empty.result.display_order == ()
    assert empty.result.technical_policy_id == "technical-discovery-v1"


@pytest.mark.parametrize("case", ["duplicate", "outside", "plan_mismatch", "row_subject"])
def test_invalid_universe_or_row_stops(case, tmp_path):
    fixed = _seed(tmp_path / "runs")
    metadata = GenerationMetadata.model_validate(fixed.model_dump(exclude={"models"}))
    tickers = tuple(t for t, plan in metadata.subject_plan.items() if plan.scope == "stock")
    rows = {}
    if case == "duplicate":
        tickers += (tickers[0],)
    elif case == "outside":
        rows = {"9999": row("9999")}
    elif case == "plan_mismatch":
        tickers = tickers[:-1]
    else:
        rows = {tickers[0]: row("9999")}
    with pytest.raises(ValueError, match="screening"):
        owner.evaluate_technical_financial_rows(
            metadata, tickers=tickers, rows=rows, policy=TechnicalFinancialPolicy()
        )


def test_financial_only_input_is_evaluated_and_all_inputs_are_unchanged(tmp_path):
    fixed = _seed(tmp_path / "runs")
    source = fixed.model_dump_json()
    selected = evaluate_fixed(fixed)
    rows = {row.ticker: row for row in selected.result.rows}
    assert rows["5678"].technical["state"] == "unavailable"
    assert rows["5678"].financial_gate["state"] == "research_candidate"
    assert "5678" in selected.result.display_order
    assert fixed.model_dump_json() == source
    copy = deepcopy(selected)
    copy.result.rows[0].technical["routes"]["market_outperformance"]["state"] = "changed"
    assert (
        selected.result.rows[0].technical["routes"]["market_outperformance"]["state"] == "matched"
    )


@pytest.mark.parametrize(
    ("growth", "technical", "financial", "expected"),
    [
        ("research_candidate", False, False, "growth_research"),
        ("research_candidate", True, True, "growth_research"),
        ("input_unresolved", False, True, "input_review"),
        ("input_unresolved", True, False, "input_review"),
        ("input_unresolved", False, False, "no_current_hypothesis"),
        ("not_selected", True, True, "alternative_financial_research"),
        ("not_selected", False, True, "alternative_financial_research"),
        ("not_selected", True, False, "no_current_hypothesis"),
    ],
)
def test_financial_lanes_do_not_overwrite_growth_failure(growth, technical, financial, expected):
    assert (
        financial_screening_lane(
            growth_state=growth, technical_match=technical, financial_path_match=financial
        )
        == expected
    )
