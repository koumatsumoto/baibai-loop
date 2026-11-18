"""Read excluded discovery reasons without restoring them to a candidate lane."""

from dataclasses import replace

import pytest
from pydantic import TypeAdapter
from tests.engine.investment_judgment.workflow.test_technical_financial_context import args, seed
from tests.engine.machine_analysis.screening.test_technical_financial import row

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.workflow.selection import ResearchRequest
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)


def excluded(ticker, *, state="not_selected", technical=("trend", "activity")):
    value = row(ticker, technical, state)
    return replace(
        value,
        screening_lane="no_current_hypothesis",
        financial_gate={
            **value.financial_gate,
            "routes": {
                "growth": {"failures": ["profit"], "missing": ["cash"]},
                "recovery": {"failures": ["profit"], "missing": ["cash", "basis"]},
            },
            "checks": {
                "profit": {"value": -10, "state": "not_met"},
                "cash": {"value": None, "state": "unavailable"},
            },
        },
    )


def test_finite_diagnostic_counts_companies_not_routes_and_never_changes_candidates():
    from baibai_engine.investment_judgment.workflow.screening_brief import (
        technical_financial_exclusion_diagnostic,
    )

    rows = (
        excluded("3001"),
        row("1000"),
        excluded("3002"),
        excluded("3003", state="input_unresolved"),
        excluded("4000", technical=()),
        row("2000", state="input_unresolved"),
    )
    result = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(page_size=2), "T", "G", rows, ("1000", "2000"), {}
    )
    codec = TypeAdapter(TechnicalFinancialSelection)
    before = codec.dump_json(result)
    pages = [technical_financial_exclusion_diagnostic(result, page=p) for p in range(3)]
    assert [[r["ticker"] for r in p["rows"]] for p in pages] == [["3001", "3002"], ["3003"], []]
    assert [p["page"]["next"] for p in pages] == [1, None, None]
    assert pages[0]["summary"] == {
        "saved_stock_rows": 6,
        "technical_matched_stocks": 5,
        "excluded_stocks": 3,
        "financial_gate_states": {"input_unresolved": 1, "not_selected": 2},
        "growth_route_failure_company_counts": {"profit": 3},
        "growth_route_missing_company_counts": {"basis": 3, "cash": 3},
        "reason_counts_are_nonadditive": True,
    }
    assert pages[0]["rows"][0] == TypeAdapter(type(rows[0])).dump_python(rows[0], mode="json")
    assert result.tickers == ("1000", "2000")
    assert codec.dump_json(result) == before
    with pytest.raises(ValueError, match="nonnegative"):
        technical_financial_exclusion_diagnostic(result, page=-1)


def test_empty_diagnostic_is_not_an_investment_opportunity_verdict():
    from baibai_engine.investment_judgment.workflow.screening_brief import (
        technical_financial_exclusion_diagnostic,
    )

    result = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(), "T", "G", (row("1000"),), ("1000",), {}
    )
    result = technical_financial_exclusion_diagnostic(result)
    assert result["rows"] == []
    assert result["summary"]["excluded_stocks"] == 0
    assert result["page"]["next"] is None
    assert (
        result["interpretation"]
        == "saved_technical_matches_without_current_financial_hypothesis_not_investment_rejection"
    )


@pytest.mark.parametrize("method", [f"technical-financial-discovery-v{n}" for n in range(1, 6)])
def test_context_diagnostic_uses_saved_policy_and_does_not_load_excluded_models(
    tmp_path, monkeypatch, method
):
    import baibai_engine.machine_analysis.generation.query as generation

    path = tmp_path / "runs"
    _, selected, now = seed(path)
    from baibai_engine.machine_analysis.screening.selection_query import read_publication

    publication = read_publication(path=path, selection_id="technical-policy")
    historical = replace(
        selected,
        result=replace(
            selected.result, policy=TechnicalFinancialPolicy(method=method, page_size=2)
        ),
    )
    publication = replace(publication, selection=historical)
    monkeypatch.setattr(
        "baibai_engine.machine_analysis.screening.selection_query.read_publication",
        lambda **kwargs: publication,
    )
    before = path.read_bytes()
    normal = load_generated_selection_context(**args(path, now), screening_lane="growth_research")
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
    diagnostic = load_generated_selection_context(**args(path, now), screening_excluded=True)
    assert diagnostic["candidate_tickers"] == []
    assert diagnostic["screening"]["cards"] == []
    assert diagnostic["screening"]["active_lane"] is None
    assert diagnostic["screening"]["policy"]["method"] == method
    assert diagnostic["generation_id"] == normal["generation_id"]
    assert diagnostic["input_known_at"] == normal["input_known_at"]
    assert diagnostic["screening_selection_id"] == normal["screening_selection_id"]
    diagnostic_rows = diagnostic["screening"]["excluded_diagnostic"]["rows"]
    assert [r["ticker"] for r in diagnostic_rows] == ["2345"]
    assert diagnostic_rows[0]["financial_gate"]["checks"]["positive_net_assets"]["value"] == -10
    assert all("2345" not in subjects for subjects in calls)
    assert diagnostic["model_inputs"]["securities"] == {}
    assert normal == load_generated_selection_context(
        **args(path, now), screening_lane="growth_research"
    )
    assert "excluded_diagnostic" not in normal["screening"]
    detail = load_generated_selection_context(**args(path, now), comparison_tickers=("2345",))
    assert detail["screening"]["comparison_rows"] == diagnostic_rows
    assert "company_growth" in detail["model_inputs"]["securities"]["2345"]["models"]
    assert path.read_bytes() == before
    assert not (tmp_path / "absent-app").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"screening_lane": "input_review"},
        {
            "request": ResearchRequest(
                text="specific", scope="specified_stocks", requested_tickers=("2345",)
            )
        },
    ],
)
def test_diagnostic_rejects_ambiguous_scope_before_read(tmp_path, change):
    from datetime import UTC, datetime

    kwargs = args(tmp_path / "absent", datetime.now(UTC)) | change
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError, match="diagnostic requires discovery and no lane override"):
        load_generated_selection_context(**kwargs, screening_excluded=True)
    assert set(tmp_path.iterdir()) == before


def test_cli_passes_explicit_diagnostic_and_page(tmp_path, monkeypatch, capsys):
    from datetime import UTC, datetime

    import baibai_engine.cli.investment_judgment as cli

    captured = []

    def load(**kwargs):
        captured.append(kwargs)
        return {"status": "ready", "candidate_tickers": []}

    monkeypatch.setattr(cli, "load_generated_selection_context", load)
    assert (
        main(
            [
                "selection-context",
                "--request",
                "絞込みの反例を確認",
                "--scope",
                "discover_new",
                "--screening-excluded",
                "--screening-page",
                "2",
                "--db",
                str(tmp_path / "absent"),
            ],
            now=datetime.now(UTC),
        )
        == 0
    )
    assert captured[0]["screening_excluded"] is True
    assert captured[0]["screening_page"] == 2
    assert '"candidate_tickers": []' in capsys.readouterr().out
