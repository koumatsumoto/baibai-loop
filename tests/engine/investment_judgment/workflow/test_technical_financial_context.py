"""保存したT/G政策を閲覧・選択・再開する。後日の再評価で入力を補わない。"""

from datetime import timedelta

import pytest
from tests.engine.machine_analysis.screening.test_technical_financial import row
from tests.helpers.archived_technical_financial import (
    SIX_STOCK_ORDER,
    stored_publication,
    stored_row,
)
from tests.helpers.generated_selection import generated_draft_for
from tests.helpers.model_generation import publish_generation
from tests.helpers.research_selection import selected_target
from tests.helpers.technical_financial import _seed

from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


def seed(path):
    fixed = _seed(path)
    fixed = fixed.model_copy(deep=True, update={"generation_id": "technical-context-fixed"})
    for model in fixed.models:
        if model.model_id == "company_growth":
            for period in model.payload["periods"]:
                for observation in period["observations"]:
                    if observation["measurement"] == "reported":
                        for metric in (
                            "parent_profit",
                            "reported_basic_eps",
                            "operating_cash_flow",
                        ):
                            observation["metrics"].setdefault(
                                metric, {"value": None, "reason": "not_observed", "documents": []}
                            )
    publish_generation(fixed, path=path)
    # Synthetic *published* facts, independent of the models returned on an explicit detail read.
    common = {
        "observation_failures": {},
        "market_relative_benchmark": "1321",
        "price_context": {"ready": True},
        "expected_price_date": "2026-11-13",
    }
    matched = {**common, "state": "matched", "matched_routes": ["trend_continuation"]}
    growth = {"state": "research_candidate"}
    excluded = {
        "state": "not_selected",
        "checks": {"positive_net_assets": {"value": -10.0, "state": "not_met"}},
    }
    rows = (
        stored_row("1234", lane="growth_research", technical=matched, gate=growth),
        stored_row("2345", lane="no_current_hypothesis", technical=matched, gate=excluded),
        stored_row(
            "3456",
            lane="input_review",
            technical=matched,
            gate={"missing_models": ["company_growth"]},
        ),
        stored_row("4567", lane="growth_research", technical=common, gate=growth),
        stored_row("5678", lane="growth_research", technical=common, gate=growth),
        stored_row(
            "6789",
            lane="input_review",
            technical=matched,
            gate={"missing_models": ["company_financials", "company_growth"]},
        ),
    )
    selected = stored_publication(fixed.generation_id, rows, order=SIX_STOCK_ORDER)
    ScreeningSelectionStore(path).publish("technical-policy", selected)
    publication = read_publication(path=path, selection_id="technical-policy")
    now = publication.published_at + timedelta(seconds=1)
    return fixed, selected, now


def args(path, now):
    return dict(
        request=ResearchRequest(text="Tから深掘り", scope="discover_new"),
        generation_id=None,
        screening_selection_id="technical-policy",
        research_as_of=None,
        known_at=now,
        runs_path=path,
        db_path=path.parent / "absent-app",
    )


def test_pages_cover_all_without_renaming_unresolved_or_loading_candidate_models(tmp_path):
    path = tmp_path / "runs"
    _, selected, now = seed(path)
    before = path.read_bytes()
    first = load_generated_selection_context(**args(path, now), screening_lane="growth_research")
    second = load_generated_selection_context(
        **args(path, now), screening_lane="growth_research", screening_page=1
    )
    third = load_generated_selection_context(**args(path, now), screening_lane="input_review")
    empty = load_generated_selection_context(
        **args(path, now), screening_lane="growth_research", screening_page=2
    )
    visible = first["candidate_tickers"] + second["candidate_tickers"] + third["candidate_tickers"]
    assert set(visible) == set(selected.result.display_order)
    assert first["screening"]["page"]["next"] == 1
    assert second["screening"]["page"]["next"] is None
    assert third["screening"]["page"]["next"] is None
    assert empty["candidate_tickers"] == []
    assert first["model_inputs"]["securities"] == {}
    assert first["screening"]["summary"]["planned_stocks"] == 6
    assert (
        first["candidate_order_semantics"] == "saved_route_round_robin_page_not_investment_ranking"
    )
    assert not (path.parent / "absent-app").exists()
    assert path.read_bytes() == before


@pytest.mark.parametrize("explicit_id", [False, True])
def test_context_reads_one_fixed_publication_without_duplicate_full_result(
    tmp_path, monkeypatch, explicit_id
):
    import baibai_engine.machine_analysis.screening.selection_query as query

    path = tmp_path / "runs"
    fixed, _, now = seed(path)
    original = query.read_publication
    calls = []

    def observe(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(query, "read_publication", observe)
    arguments = args(path, now)
    if not explicit_id:
        arguments["screening_selection_id"] = None
        with pytest.raises(ValueError, match="explicit legacy screening selection"):
            load_generated_selection_context(**arguments, screening_lane="growth_research")
        overview = load_generated_selection_context(**arguments, comparison_tickers=("2345",))
        assert overview["status"] == "inputs_required"
        assert overview["screening_selection_id"] == "technical-policy"
        assert overview["missing_inputs"] == [{"code": "unindexed_legacy_publication"}]
        assert not calls
        return
    context = load_generated_selection_context(**arguments, screening_lane="growth_research")
    assert len(calls) == 1
    assert calls[0]["known_at"] == now
    assert calls[0]["selection_id"] == "technical-policy"
    assert context["screening_selection_id"] == "technical-policy"
    assert context["generation_id"] == fixed.generation_id
    assert context["candidate_tickers"]
    assert not (path.parent / "absent-app").exists()


def test_nonselected_and_capacity_outside_detail_reads_original_policy_and_full_models(tmp_path):
    path = tmp_path / "runs"
    fixed, _, now = seed(path)

    context = load_generated_selection_context(
        **args(path, now), comparison_tickers=("2345", "6789")
    )
    records = {row["ticker"]: row for row in context["screening"]["comparison_rows"]}
    assert records["2345"]["financial_gate"]["state"] == "not_selected"
    assert records["6789"]["financial_gate"]["state"] == "input_unresolved"
    growth = next(
        model.payload
        for model in fixed.models
        if model.subject == "2345" and model.model_id == "company_growth"
    )
    assert context["model_inputs"]["securities"]["2345"]["models"]["company_growth"] == growth
    assert context["generation_id"] == fixed.generation_id


@pytest.mark.parametrize(
    ("target", "state"), [("3456", "input_unresolved"), ("5678", "research_candidate")]
)
def test_new_selection_prepares_and_resumes_from_saved_inputs_without_run_store(
    tmp_path, target, state
):
    path = tmp_path / "runs"
    fixed, _, now = seed(path)
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(text="未確定の比較理由を調べる", scope="discover_new"),
        research_as_of=fixed.as_of,
        input_known_at=now,
        generation_id=fixed.generation_id,
        screening_selection_id="technical-policy",
        selection_summary="G未確定を合格ではなく予備調査として選ぶ",
        targets=(selected_target(target),),
    )
    prepared = prepare_selected_workspace(
        draft=draft,
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        runs_path=path,
        now=now,
    )
    saved = selection_binding(OperationService(tmp_path / "app").get(prepared.operation_id).payload)
    digest = selection_sha256(saved)
    projection = project_research_inputs(saved)
    saved_result = projection["screening_selection"]["result"]
    assert "full_result_ref" not in saved_result
    assert saved_result["comparison_rows"][0]["financial_gate"]["state"] == state
    assert tuple(r.ticker for r in saved.inputs.screening_selection.result.rows) == tuple(
        t.ticker for t in saved.targets
    )
    path.unlink()
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        now=now + timedelta(days=1),
    )
    assert resumed.operation_id == prepared.operation_id
    assert (
        selection_sha256(
            selection_binding(OperationService(tmp_path / "app").get(prepared.operation_id).payload)
        )
        == digest
    )
    assert project_research_inputs(saved) == projection


def test_publication_cutoff_generation_and_page_are_enforced(tmp_path):
    path = tmp_path / "runs"
    _, _, now = seed(path)
    publication = read_publication(path=path, selection_id="technical-policy")
    with pytest.raises(ValueError, match="not yet published"):
        load_generated_selection_context(
            **args(path, publication.published_at - timedelta(microseconds=1))
        )
    changed = args(path, now)
    changed["generation_id"] = "another-generation"
    with pytest.raises(ValueError, match="differs"):
        load_generated_selection_context(**changed)
    with pytest.raises(ValueError, match="nonnegative"):
        load_generated_selection_context(**args(path, now), screening_page=-1)


def test_display_capacity_never_removes_saved_candidates():
    rows = tuple(row(str(1000 + index), ("a" if index % 2 else "b",)) for index in range(73))
    result = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(),
        "T",
        "G",
        rows,
        tuple(r.ticker for r in reversed(rows)),
        {"planned_stocks": 73},
    )
    pages = [technical_financial_screening_brief(result, page=page) for page in range(3)]
    assert [len(page["cards"]) for page in pages] == [36, 36, 1]
    assert sum(len(page["cards"]) for page in pages) == len(result.tickers) == 73
    pages[0]["cards"][0]["technical"]["matched_routes"].append("mutated")
    assert all("mutated" not in item.technical["matched_routes"] for item in result.rows)


def test_alternative_lane_is_explicit_and_keeps_known_failure():
    from dataclasses import replace

    original = row("1234", ("activity",), state="not_selected")
    alternative = replace(
        original,
        financial_gate={
            **original.financial_gate,
            "checks": {
                "quarter_operating_profit_increase": {
                    "state": "not_met",
                    "value": -50,
                    "reason": None,
                },
            },
        },
        financial_paths={"matched_routes": ["business_expansion"], "paths": []},
        screening_lane="alternative_financial_research",
    )
    result = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(),
        "T",
        "G",
        (alternative,),
        (alternative.ticker,),
        {},
    )
    primary = technical_financial_screening_brief(result)
    assert primary["cards"] == []
    assert primary["lanes"]["alternative_financial_research"]["count"] == 1
    assert (
        "--screening-lane alternative_financial_research"
        in primary["lanes"]["alternative_financial_research"]["read"]
    )
    detail = technical_financial_screening_brief(result, lane="alternative_financial_research")
    card = detail["cards"][0]
    assert card["financial_gate"]["state"] == "not_selected"
    assert card["financial_gate"]["checks"]["quarter_operating_profit_increase"]["value"] == -50
    assert card["screening_lane"] == "alternative_financial_research"


def test_cli_reads_requested_lane_without_selecting(tmp_path, capsys):
    import json

    from baibai_engine.cli.investment_judgment import main

    path = tmp_path / "runs"
    _, _, now = seed(path)
    before = path.read_bytes()
    assert (
        main(
            [
                "selection-context",
                "--request",
                "予備調査を比較",
                "--scope",
                "discover_new",
                "--screening-selection-id",
                "technical-policy",
                "--runs-path",
                str(path),
                "--db",
                str(tmp_path / "absent-app"),
                "--screening-lane",
                "input_review",
            ],
            now=now,
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["screening"]["active_lane"] == "input_review"
    assert set(output["candidate_tickers"]) == {"3456", "6789"}
    assert all(
        card["financial_gate"]["state"] == "input_unresolved"
        for card in output["screening"]["cards"]
    )
    assert path.read_bytes() == before
    assert not (tmp_path / "absent-app").exists()


@pytest.mark.parametrize("indexed", [False, True])
def test_case_pins_explicit_contrast_and_scope_without_promoting_or_using_later_generation(
    tmp_path,
    indexed,
):
    import gzip
    import sqlite3
    from hashlib import sha256

    from baibai_engine.investment_judgment.workflow.screening_brief import saved_discovery_context

    runs, app, workspace = tmp_path / "runs", tmp_path / "app", tmp_path / "case"
    fixed, source, now = seed(runs)
    selection_id = "technical-policy"
    if indexed:
        from baibai_engine.machine_analysis.screening.policies.discovery import (
            evaluate_discovery_rows,
            project_discovery_row,
        )

        stocks = source.universe_tickers
        source = evaluate_discovery_rows(
            fixed,
            tickers=stocks,
            rows={
                t: project_discovery_row(
                    t, models=tuple(m for m in fixed.models if m.subject == t), metadata=fixed
                )
                for t in stocks
            },
        )
        selection_id = f"selection-{fixed.generation_id}-opportunity-discovery-v1"
        ScreeningSelectionStore(runs).publish(selection_id, source)
        now = read_publication(path=runs, selection_id=selection_id).published_at + timedelta(
            seconds=1
        )
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(text="有限の根拠を比べる", scope="discover_new"),
        research_as_of=fixed.as_of,
        input_known_at=now,
        generation_id=fixed.generation_id,
        screening_selection_id=selection_id,
        selection_summary="成長仮説と未観測の対照",
        targets=(selected_target("1234"),),
        comparison_tickers=("5678", "9999"),
    )
    result = prepare_selected_workspace(
        draft=draft, db_path=app, workspace=workspace, runs_path=runs, now=now
    )
    saved = selection_binding(OperationService(app).get(result.operation_id).payload)
    screen = saved.inputs.screening_selection
    with sqlite3.connect(runs) as c:
        wire = gzip.decompress(
            c.execute(
                "SELECT payload_gzip FROM screening_selection WHERE selection_id=?",
                (selection_id,),
            ).fetchone()[0]
        )
    assert screen.source_sha256 == sha256(wire).hexdigest() != result.selection_sha256
    assert screen.universe_tickers == source.universe_tickers
    assert {t for group in screen.population_groups for t in group.tickers} == set(
        source.universe_tickers
    )
    row_id = (lambda row: row["ticker"]) if indexed else (lambda row: row.ticker)
    assert {row_id(x) for x in screen.result.rows} == {"1234", "5678"}
    assert screen.result.rows == tuple(
        r for r in source.result.rows if row_id(r) in {"1234", "5678"}
    )
    assert result.selected_tickers == ("1234",)
    assert saved.inputs.generation.result("company_growth", "5678") == fixed.result(
        "company_growth", "5678"
    )
    assert saved.inputs.generation.result("company_growth", "2345") is None
    assert set(saved.inputs.saved_company_context) == {"1234"}
    projected = project_research_inputs(saved)["screening_selection"]
    if indexed:
        assert "candidate_count" not in projected
        assert projected["explorable_count_not_investment_eligibility"] == len(
            source.universe_tickers
        )
        assert all(type(g["models_recorded"]) is bool for g in projected["population"])
    assert projected["outside_published_universe"] == ["9999"]
    assert projected["no_saved_screening_detail"] == ["9999"]
    details = saved_discovery_context(saved, selection_hash=result.selection_sha256)
    assert [x["ticker"] for x in details["rows"]] == ["1234"]
    assert [x["ticker"] for x in details["comparison_rows"]] == ["5678"]
    if indexed:
        assert details["comparison_rows"][0] == next(
            r for r in source.result.rows if r["ticker"] == "5678"
        )
        assert details["policy"] == {"method": "opportunity-discovery-v1"}
        assert "technical_policy_id" not in details
    assert details["comparisons_not_in_saved_rows"] == ["9999"]
    altered = saved.model_dump(mode="json")
    altered["inputs"]["screening_selection"]["result"]["rows"][0]["name"] = "later disclosure"
    from baibai_engine.investment_judgment.workflow.selection import (
        parse_selection,
        selection_sha256,
    )

    with pytest.raises(ValueError, match="supplied hash"):
        OperationService(tmp_path / "other", runs_path=runs).start(
            generated_draft_for(parse_selection(altered)).model_copy(
                update={"expected_selection_sha256": selection_sha256(parse_selection(altered))}
            ),
            now=now,
        )
    assert OperationService(tmp_path / "other").active() is None
    runs.rename(tmp_path / "unavailable")
    replay = resume_selected_workspace(
        operation_id=result.operation_id, db_path=app, workspace=tmp_path / "resumed", now=now
    )
    assert replay.selection_sha256 == result.selection_sha256
    assert (replay.workspace / "domain-inputs.yaml").read_bytes() == (
        workspace / "domain-inputs.yaml"
    ).read_bytes()
    with pytest.raises(ValueError, match="distinct non-target"):
        type(draft).model_validate({**draft.model_dump(), "comparison_tickers": ["1234"]})
    with pytest.raises(ValueError, match="comparison evidence exceeds"):
        type(draft).model_validate(
            {**draft.model_dump(), "comparison_tickers": ["2001", "2002", "2003", "2004"]}
        )
