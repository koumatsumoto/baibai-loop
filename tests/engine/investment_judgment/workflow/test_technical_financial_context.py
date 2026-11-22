"""指定企業の保存行を原文と照合し、有限のCaseを同じ根拠から再開する。"""

from datetime import timedelta

import pytest
from tests.helpers.archived_technical_financial import (
    SIX_STOCK_ORDER,
    stored_publication,
    stored_row,
)
from tests.helpers.model_generation import publish_generation
from tests.helpers.research_selection import selected_target
from tests.helpers.technical_financial import _seed

from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
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


@pytest.mark.parametrize("indexed", [False, True])
def test_case_pins_explicit_contrast_and_scope_without_promoting_or_using_later_generation(
    tmp_path,
    indexed,
):
    import gzip
    import sqlite3
    from hashlib import sha256

    from baibai_engine.investment_judgment.trade_proposal.discovery_context import (
        saved_discovery_context,
    )
    from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
    from baibai_engine.investment_judgment.workflow.operation.service import OperationConflictError

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
        selection_id = f"selection-{fixed.generation_id}-opportunity-discovery-v3"
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
        assert details["policy"] == {"method": "opportunity-discovery-v3"}
        assert "technical_policy_id" not in details
    assert details["comparisons_not_in_saved_rows"] == ["9999"]
    altered = saved.model_dump(mode="json")
    altered["inputs"]["screening_selection"]["result"]["rows"][0]["name"] = "later disclosure"
    with pytest.raises(OperationConflictError, match="screening selection differs"):
        OperationService(tmp_path / "other", runs_path=runs).start(
            session_kind="capital-allocation",
            as_of=fixed.as_of,
            started_at=now,
            payload=OperationPayload(checkpoint="test", artifacts=(altered,)),
        )
    assert not (tmp_path / "other").exists()
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
