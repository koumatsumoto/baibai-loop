"""候補外の比較不足を発見する。未確定を候補や経済的な見送りへ変えない。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter
from tests.engine.investment_judgment.workflow.test_technical_financial_context import args, seed
from tests.engine.machine_analysis.screening.test_technical_financial import evaluate_fixed, row
from tests.helpers.model_generation import publish_generation

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_exclusion_diagnostic,
)
from baibai_engine.investment_judgment.workflow.selection import ResearchRequest
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialRow,
    TechnicalFinancialSelection,
    discovery_display_order,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


def unresolved(ticker, *, technical_state="not_matched", failure=False, missing_model=False):
    value = row(ticker, (), "input_unresolved")
    return replace(
        value,
        technical={**value.technical, "state": technical_state},
        financial_gate={
            **value.financial_gate,
            "missing_models": ["company_growth"] if missing_model else [],
            "routes": {
                "profit_growth": {
                    "state": "not_matched" if failure else "unresolved",
                    "failures": ["positive_quarter_profit"] if failure else [],
                    "missing": ["comparison"],
                },
                "operating_recovery": {
                    "state": "unresolved",
                    "failures": [],
                    "missing": ["comparison"],
                },
            },
            "checks": {
                "positive_quarter_profit": {
                    "value": -10 if failure else 10,
                    "state": "not_met" if failure else "met",
                },
                "comparison": {
                    "value": None,
                    "state": "unavailable",
                    "reason": "metric_not_observed"
                    if missing_model
                    else "statement_basis_incomparable",
                },
            },
        },
        financial_paths={
            "matched_routes": [],
            "paths": [
                {
                    "path_id": "earning_power",
                    "eligible": False,
                    "price_value": None,
                    "business_value": 0.1,
                }
            ],
        },
    )


def test_unresolved_scope_finds_nontechnical_rows_with_failures_and_missing_models_intact():
    rows = (
        unresolved("4000", technical_state="unavailable", missing_model=True),
        unresolved("3000", failure=True),
        unresolved("2000"),
        row("1000"),
        row("5000", ("trend",), "input_unresolved"),
        row("6000", ("trend",), "not_selected"),
    )
    result = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(page_size=2), "T", "G", rows, discovery_display_order(rows), {}
    )
    codec = TypeAdapter(TechnicalFinancialSelection)
    before = codec.dump_json(result)
    legacy = technical_financial_exclusion_diagnostic(result)
    pages = [
        technical_financial_exclusion_diagnostic(result, page=p, scope="financial_unresolved")
        for p in range(3)
    ]
    assert [r["ticker"] for r in legacy["rows"]] == ["6000"]
    assert [[r["ticker"] for r in p["rows"]] for p in pages] == [["2000", "3000"], ["4000"], []]
    assert [p["page"]["next"] for p in pages] == [1, None, None]
    summary = pages[0]["summary"]
    assert summary["saved_stock_rows"] == 6
    assert summary["excluded_stocks"] == 3
    assert summary["financial_gate_states"] == {"input_unresolved": 3}
    assert summary["technical_states"] == {"not_matched": 2, "unavailable": 1}
    assert summary["growth_route_failure_company_counts"] == {"positive_quarter_profit": 1}
    assert summary["growth_route_missing_company_counts"] == {"comparison": 3}
    expected = {
        r.ticker: TypeAdapter(TechnicalFinancialRow).dump_python(r, mode="json") for r in rows
    }
    for page in pages:
        assert (
            "--screening-excluded financial_unresolved --screening-page" in page["next_page_read"]
        )
        for value in page["rows"]:
            assert value == expected[value["ticker"]]
            assert value["screening_lane"] == "no_current_hypothesis"
    assert pages[0]["scope"] == "financial_unresolved"
    assert "not_research_candidates" in pages[0]["interpretation"]
    assert codec.dump_json(result) == before
    assert technical_financial_exclusion_diagnostic(result) == legacy


@pytest.mark.parametrize(
    "method",
    [
        "technical-financial-discovery-v1",
        "technical-financial-discovery-v11",
        "technical-financial-discovery-v12",
    ],
)
def test_normal_saved_context_preserves_cutoff_order_and_only_explicit_detail_loads_models(
    tmp_path, monkeypatch, method
):
    import baibai_engine.machine_analysis.generation.query as generation

    path = tmp_path / "runs"
    fixed, _, _ = seed(path)
    fixed = fixed.model_copy(deep=True, update={"generation_id": "unresolved-diagnostic-fixed"})
    for model in fixed.models:
        if model.subject == "3456" and model.model_id == "technical_analysis":
            model.payload["price_path"]["current_row_state"] = "no_trade"
    publish_generation(fixed, path=path)
    selected = evaluate_fixed(fixed)
    selected = replace(
        selected,
        result=replace(
            selected.result, policy=TechnicalFinancialPolicy(method=method, page_size=2)
        ),
    )
    ScreeningSelectionStore(path).publish("unresolved-policy", selected)
    publication = read_publication(path=path, selection_id="unresolved-policy")
    now = publication.published_at + timedelta(seconds=1)
    arguments = args(path, now) | {"screening_selection_id": "unresolved-policy"}
    before = path.read_bytes()
    normal = load_generated_selection_context(**arguments)
    legacy = load_generated_selection_context(**arguments, screening_excluded=True)
    calls = []
    original_read = generation.read_model_inputs

    def read(*a, **kw):
        calls.append(kw["subjects"])
        return original_read(*a, **kw)

    monkeypatch.setattr(generation, "read_model_inputs", read)

    def forbidden(*a, **kw):
        pytest.fail("diagnostic must not recompute policies")

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.screening.policies.technical_financial_selection.project_technical_financial_row",
        forbidden,
    )
    diagnostic = load_generated_selection_context(
        **arguments, screening_excluded=True, screening_excluded_scope="financial_unresolved"
    )
    assert diagnostic["candidate_tickers"] == []
    assert diagnostic["screening"]["cards"] == []
    assert diagnostic["screening"]["active_lane"] is None
    assert diagnostic["screening"]["policy"]["method"] == method
    for key in ("generation_id", "input_known_at", "research_as_of", "screening_selection_id"):
        assert diagnostic[key] == normal[key]
    diagnostic_rows = diagnostic["screening"]["excluded_diagnostic"]["rows"]
    assert [r["ticker"] for r in diagnostic_rows] == ["3456"]
    assert diagnostic_rows[0]["financial_gate"]["missing_models"] == ["company_growth"]
    assert diagnostic_rows[0]["screening_lane"] == "no_current_hypothesis"
    assert all("3456" not in subjects for subjects in calls)
    assert diagnostic["model_inputs"]["securities"] == {}
    assert normal == load_generated_selection_context(**arguments)
    assert legacy == load_generated_selection_context(**arguments, screening_excluded=True)
    detail = load_generated_selection_context(**arguments, comparison_tickers=("3456",))
    assert detail["screening"]["comparison_rows"] == diagnostic_rows
    assert "company_growth" in detail["model_inputs"]["securities"]["3456"]["not_generated"]
    with pytest.raises(ValueError, match="not yet published"):
        load_generated_selection_context(
            **(arguments | {"known_at": publication.published_at - timedelta(microseconds=1)}),
            screening_excluded=True,
            screening_excluded_scope="financial_unresolved",
        )
    assert path.read_bytes() == before
    assert not (tmp_path / "absent-app").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"screening_excluded": False},
        {"screening_lane": "input_review"},
        {"screening_excluded_scope": "unknown"},
        {
            "request": ResearchRequest(
                text="specific", scope="specified_stocks", requested_tickers=("3456",)
            )
        },
    ],
)
def test_new_scope_rejects_ambiguous_request_before_read(tmp_path, change):
    kwargs = (
        args(tmp_path / "absent", datetime.now(UTC))
        | {
            "screening_excluded": True,
            "screening_excluded_scope": "financial_unresolved",
        }
        | change
    )
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError, match="diagnostic"):
        load_generated_selection_context(**kwargs)
    assert set(tmp_path.iterdir()) == before


def test_empty_unresolved_scope_does_not_assert_financial_completeness():
    result = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(), "T", "G", (row("1000"),), ("1000",), {}
    )
    output = technical_financial_exclusion_diagnostic(result, scope="financial_unresolved")
    assert output["rows"] == []
    assert output["page"]["next"] is None
    assert output["summary"]["excluded_stocks"] == 0
    assert output["summary"]["technical_states"] == {}
    assert output["interpretation"] == (
        "saved_financially_unresolved_rows_outside_current_lanes_not_research_candidates"
    )
    with pytest.raises(ValueError, match="diagnostic scope"):
        technical_financial_exclusion_diagnostic(result, scope="unknown")


@pytest.mark.parametrize("option", [[], ["technical_matched"], ["financial_unresolved"]])
def test_cli_legacy_flag_and_explicit_scope_keep_saved_id_cutoff_page(
    tmp_path, monkeypatch, capsys, option
):
    import baibai_engine.cli.investment_judgment as cli

    captured = []
    monkeypatch.setattr(
        cli,
        "load_generated_selection_context",
        lambda **kw: captured.append(kw) or {"status": "ready", "candidate_tickers": []},
    )
    now = datetime.now(UTC)
    assert (
        cli.main(
            [
                "selection-context",
                "--request",
                "候補外の比較不足を調べる",
                "--scope",
                "discover_new",
                "--screening-selection-id",
                "saved-fixed",
                "--known-at",
                now.isoformat(),
                "--screening-excluded",
                *option,
                "--screening-page",
                "2",
                "--db",
                str(tmp_path / "absent"),
            ],
            now=now,
        )
        == 0
    )
    assert captured[0]["screening_excluded"] is True
    assert captured[0]["screening_excluded_scope"] == (option[0] if option else "technical_matched")
    assert captured[0]["screening_page"] == 2
    assert captured[0]["screening_selection_id"] == "saved-fixed"
    assert captured[0]["known_at"] == now
    assert '"candidate_tickers": []' in capsys.readouterr().out
