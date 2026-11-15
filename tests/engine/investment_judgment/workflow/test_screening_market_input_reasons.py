"""候補カードの市場入力不足を、不利な観測や古い価格へ変換しない。"""

from copy import deepcopy
from dataclasses import replace

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_technical_financial import row

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)

FIELDS = (
    "observation_failures",
    "market_relative_benchmark",
    "price_context",
    "expected_price_date",
)


def saved(*, lane="growth_research", method="technical-financial-discovery-v5"):
    state = {
        "growth_research": "research_candidate",
        "input_review": "input_unresolved",
        "alternative_financial_research": "not_selected",
    }[lane]
    base = row("1234", state=state)
    technical = {
        **base.technical,
        "state": "unavailable",
        "matched_routes": [],
        "routes": {
            "market_down_resilience": {
                "state": "not_matched",
                "conditions": {"own_positive": False, "excess_positive": None},
                "failed_conditions": ["own_positive"],
                "missing_conditions": ["excess_positive"],
            },
        },
        "price_context": {
            "ready": True,
            "freshness": "current",
            "latest_observed_price_date": "2026-11-13",
        },
        "expected_price_date": "2026-11-13",
        "market_relative_benchmark": "1321",
        "observation_failures": {
            "market_down_excess_return_60d": {
                "source_field": "relative.market_conditioned_60d.benchmarks.1321.down.mean_excess_return",
                "source_state": "recorded_null",
                "unavailable_reason": "recorded_null",
                "source_status": "unavailable",
                "source_unavailable_reason": "incomplete_price_window",
            },
        },
    }
    base = replace(base, technical=technical, screening_lane=lane)
    return TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method=method), "T", "G", (base,), (base.ticker,), {}
    )


@pytest.mark.parametrize(
    "lane", ["growth_research", "input_review", "alternative_financial_research"]
)
def test_card_keeps_saved_input_reason_clock_and_benchmark_without_reinterpreting(lane):
    result = saved(lane=lane)
    before = TypeAdapter(TechnicalFinancialSelection).dump_json(result)
    card = technical_financial_screening_brief(result, lane=lane)["cards"][0]
    for field in FIELDS:
        assert card["technical"][field] == result.rows[0].technical[field]
    assert card["technical"]["price_context"]["ready"] is True
    assert card["technical"]["routes"] == result.rows[0].technical["routes"]
    assert card["technical"]["state"] == "unavailable"
    assert card["technical"]["routes"]["market_down_resilience"]["conditions"] == {
        "own_positive": False,
        "excess_positive": None,
    }
    assert card["screening_lane"] == lane
    assert card["financial_gate"]["state"] == result.rows[0].financial_gate["state"]
    card["technical"]["observation_failures"].clear()
    card["technical"]["price_context"]["ready"] = False
    assert TypeAdapter(TechnicalFinancialSelection).dump_json(result) == before


@pytest.mark.parametrize("method", [f"technical-financial-discovery-v{v}" for v in range(1, 6)])
def test_historical_missing_fields_are_not_backfilled(method):
    result = saved(method=method)
    technical = deepcopy(result.rows[0].technical)
    for field in FIELDS:
        technical.pop(field)
    result = replace(result, rows=(replace(result.rows[0], technical=technical),))
    card = technical_financial_screening_brief(result)["cards"][0]
    assert all(field not in card["technical"] for field in FIELDS)
    assert card["technical"]["state"] == "unavailable"
    assert result.policy.method == method


def test_recorded_empty_failure_map_is_not_replaced_with_missing_and_exact_detail_is_unchanged():
    result = saved()
    technical = {**result.rows[0].technical, "observation_failures": {}}
    result = replace(result, rows=(replace(result.rows[0], technical=technical),))
    card = technical_financial_screening_brief(result)["cards"][0]
    assert card["technical"]["observation_failures"] == {}
    detail = technical_financial_screening_brief(result, page=None, comparison_tickers=("1234",))
    assert detail["cards"] == []
    assert detail["comparison_rows"] == [
        TypeAdapter(type(result.rows[0])).dump_python(result.rows[0], mode="json")
    ]
    assert result.tickers == ("1234",)


def test_normal_selection_context_keeps_the_saved_fields_without_loading_company_models(tmp_path):
    from tests.engine.investment_judgment.workflow.test_technical_financial_context import (
        args,
        seed,
    )

    from baibai_engine.investment_judgment.workflow.selection_query import (
        load_generated_selection_context,
    )

    path = tmp_path / "runs"
    _, selected, now = seed(path)
    before = path.read_bytes()
    context = load_generated_selection_context(**args(path, now))
    source = {row.ticker: row for row in selected.result.rows}
    assert context["screening"]["cards"]
    for card in context["screening"]["cards"]:
        for field in FIELDS:
            assert card["technical"][field] == source[card["ticker"]].technical[field]
    assert context["model_inputs"]["securities"] == {}
    assert path.read_bytes() == before
