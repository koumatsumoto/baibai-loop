"""株数確認の予備調査を同じ保存入力で読む。G・価格・採否は補わない。"""

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_financial_input_review import case
from tests.engine.machine_analysis.screening.test_unresolved_financial_input_review import (
    unresolved_case,
)
from tests.helpers.archived_technical_financial import (
    FOUR_STOCK_ORDER,
    stored_publication,
    stored_row,
)
from tests.helpers.model_generation import publish_generation
from tests.helpers.research_selection import selected_target
from tests.helpers.technical_discovery import _metadata

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
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationSubject,
    ModelGeneration,
)
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import encode_json


def seed(path, *, stale=False, unresolved=False):
    payloads = {
        "1234": case(reason="missing_capital_basis", technical=False),
        "2345": case(expansion=True, reason="missing_capital_basis", technical=False),
        "3456": case(),
        "4567": case(reason="unknown_reason", technical=False),
    }
    payloads["2345"]["company_growth"]["periods"][-1]["observations"][-1]["metrics"]["net_assets"][
        "value"
    ] = -22.0
    if stale:
        for ticker in ("1234", "2345"):
            payloads[ticker]["company_financials"]["earnings_price"].update(
                unavailable_reason="stale_price",
                price_date="2026-11-12",
                shares_ex_treasury=100.0,
                market_cap_yen=10000.0,
            )
    if unresolved:
        payloads["1234"] = unresolved_case(stale=stale)
    generated = []
    for ticker, data in payloads.items():
        data["company_growth"]["ticker"] = ticker
        for period in data["company_growth"]["periods"]:
            for observation in period["observations"]:
                if observation["measurement"] == "reported":
                    for metric in ("parent_profit", "reported_basic_eps", "operating_cash_flow"):
                        observation["metrics"].setdefault(
                            metric, {"value": None, "reason": "not_observed", "documents": []}
                        )
        generated.extend(
            GeneratedModel(
                model_id=name, subject=ticker, calculation_revision="test-v1", payload=payload
            )
            for name, payload in data.items()
        )
    metadata = _metadata().model_copy(
        update={
            "subject_plan": {
                ticker: GenerationSubject(scope="stock", model_ids=tuple(data))
                for ticker, data in payloads.items()
            }
        }
    )
    fixed = ModelGeneration(**dict(metadata), models=tuple(generated))
    publish_generation(fixed, path=path)
    reason = {
        "path_id": "earning_power",
        "reason": "stale_price" if stale else "missing_capital_basis",
        "price_date": "2026-11-12" if stale else "2026-11-13",
        "expected_price_date": "2026-11-13",
        "generation_id": fixed.generation_id,
    }
    rows = (
        stored_row(
            "1234",
            lane="input_review",
            gate={"state": "input_unresolved" if unresolved else "not_selected"},
            paths={"input_review_reasons": [reason]},
        ),
        stored_row(
            "2345",
            lane="input_review",
            gate={
                "state": "not_selected",
                "checks": {"positive_net_assets": {"state": "not_met", "value": -22.0}},
            },
            paths={"input_review_reasons": [reason]},
        ),
        stored_row("3456", lane="alternative_financial_research", gate={"state": "not_selected"}),
        stored_row("4567", lane="no_current_hypothesis", gate={"state": "not_selected"}),
    )
    selection = stored_publication(fixed.generation_id, rows, order=FOUR_STOCK_ORDER, page_size=1)
    identity = "archived-financial-review"
    ScreeningSelectionStore(path).publish(identity, selection)
    publication = read_publication(path=path, selection_id=identity)
    return fixed, selection, identity, publication.published_at + timedelta(seconds=1)


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("unresolved", [False, True])
def test_new_review_reasons_survive_pages_detail_saved_selection_and_runless_resume(
    tmp_path, stale, unresolved
):
    path = tmp_path / "runs"
    fixed, selected, identity, now = seed(path, stale=stale, unresolved=unresolved)
    original_bytes = path.read_bytes()
    kwargs = dict(
        request=ResearchRequest(text="株数基準の確認", scope="discover_new"),
        generation_id=fixed.generation_id,
        screening_selection_id=identity,
        research_as_of=None,
        known_at=now,
        runs_path=path,
        db_path=tmp_path / "app",
    )
    pages = [
        load_generated_selection_context(**kwargs, screening_lane="input_review", screening_page=p)
        for p in range(3)
    ]
    assert [p["candidate_tickers"] for p in pages] == [["2345"], ["1234"], []]
    assert len(selected.result.rows) == selected.result.summary["planned_stocks"] == 4
    assert len(selected.result.display_order) == len(set(selected.result.display_order)) == 3
    for page in pages[:2]:
        card = page["screening"]["cards"][0]
        assert card["financial_gate"]["state"] == (
            "input_unresolved" if unresolved and card["ticker"] == "1234" else "not_selected"
        )
        assert card["financial_paths"]["matched_routes"] == []
        assert card["financial_paths"]["coordinates"] == []
        reason = card["financial_paths"]["input_review_reasons"][0]
        assert reason["generation_id"] == fixed.generation_id
        assert reason["reason"] == ("stale_price" if stale else "missing_capital_basis")
        if stale:
            assert reason["price_date"] == "2026-11-12"
            assert reason["expected_price_date"] == "2026-11-13"
    detail = load_generated_selection_context(**kwargs, comparison_tickers=("1234", "2345", "4567"))
    saved_rows = {row.ticker: row for row in selected.result.rows}
    for row in detail["screening"]["comparison_rows"]:
        assert row == TypeAdapter(type(saved_rows[row["ticker"]])).dump_python(
            saved_rows[row["ticker"]], mode="json"
        )
    assert (
        detail["screening"]["comparison_rows"][1]["financial_gate"]["checks"][
            "positive_net_assets"
        ]["value"]
        == -22.0
    )
    with pytest.raises(ValueError, match="not yet published"):
        load_generated_selection_context(**{**kwargs, "known_at": now - timedelta(seconds=2)})
    with pytest.raises(ValueError, match="differs"):
        load_generated_selection_context(**{**kwargs, "generation_id": "different"})
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=kwargs["request"],
        research_as_of=fixed.as_of,
        input_known_at=now,
        generation_id=fixed.generation_id,
        screening_selection_id=identity,
        selection_summary="株数不足と既知の成長非合致を区別し、原典確認を明示選択する",
        targets=(selected_target("1234"), selected_target("2345")),
    )
    prepared = prepare_selected_workspace(
        draft=draft,
        db_path=tmp_path / "app",
        workspace=tmp_path / "workspace",
        runs_path=path,
        now=now,
    )
    operation = OperationService(tmp_path / "app").get(prepared.operation_id)
    saved = selection_binding(operation.payload)
    digest = selection_sha256(saved)
    projection = project_research_inputs(saved)
    assert (
        projection["screening_selection"]["result"]["comparison_rows"][1]["financial_gate"]["state"]
        == "not_selected"
    )
    assert path.read_bytes() == original_bytes
    path.rename(tmp_path / "runs-not-available")
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
    assert not path.exists()


def test_new_review_queue_is_company_unique_and_reaches_all_pages():
    base = stored_row("1234", lane="input_review", gate={"state": "not_selected"})
    reasons = [
        {"path_id": path, "reason": "missing_capital_basis"}
        for path in ("business_expansion", "earning_power")
    ]
    rows = tuple(
        replace(
            base,
            ticker=str(1000 + i),
            financial_paths={**base.financial_paths, "input_review_reasons": reasons},
        )
        for i in range(73)
    )
    result = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(), "T", "G", rows, tuple(r.ticker for r in rows), {}
    )
    pages = [
        technical_financial_screening_brief(result, lane="input_review", page=p) for p in range(3)
    ]
    assert [len(page["cards"]) for page in pages] == [36, 36, 1]
    assert len(set(result.display_order)) == len(result.display_order) == 73


@pytest.mark.parametrize("revision", ["v1", "v2", "v3", "v4"])
def test_historical_policy_keeps_canonical_fields_and_is_not_recomputed(tmp_path, revision):
    _fixed, current, _, _ = seed(tmp_path / "runs")
    rows = (
        tuple(
            replace(
                row,
                financial_paths={
                    key: value
                    for key, value in row.financial_paths.items()
                    if key != "input_review_reasons"
                },
                screening_lane="no_current_hypothesis"
                if "input_review_reasons" in row.financial_paths
                else row.screening_lane,
            )
            for row in current.result.rows
        )
        if revision == "v1"
        else current.result.rows
    )
    legacy = replace(
        current,
        result=replace(
            current.result,
            policy=TechnicalFinancialPolicy(method=f"technical-financial-discovery-{revision}"),
            rows=rows,
            display_order=tuple(r.ticker for r in rows),
        ),
    )
    codec = TypeAdapter(ScreeningSelection)
    payload = encode_json(codec.dump_python(legacy, mode="json"))
    assert ("input_review_reasons" in payload) is (revision != "v1")
    parsed = codec.validate_json(payload)
    assert encode_json(codec.dump_python(parsed, mode="json")) == payload
    assert bool(parsed.result.lane_tickers("input_review")) is (revision != "v1")
    assert (
        technical_financial_screening_brief(parsed.result)["policy"]["method"]
        == f"technical-financial-discovery-{revision}"
    )
    identity = f"archived-review-{revision}"
    store = ScreeningSelectionStore(tmp_path / "runs")
    store.publish(identity, legacy)
    before_retry = (tmp_path / "runs").read_bytes()
    store.publish(identity, legacy)
    assert (tmp_path / "runs").read_bytes() == before_retry
    restored = read_publication(path=tmp_path / "runs", selection_id=identity).selection
    assert encode_json(codec.dump_python(restored, mode="json")) == payload
