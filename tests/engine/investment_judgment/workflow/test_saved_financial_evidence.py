"""明示した旧記録と元の財務資料を別々に読む。欠欄・改訂・権利を再生成しない。"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from tests.engine.investment_judgment.workflow.test_technical_financial_context import args, seed
from tests.helpers.archived_technical_financial import stored_publication, stored_row
from tests.helpers.discovery_source_examples import BOOK, CASH_FLOW, FORECAST, NOTICE
from tests.helpers.model_generation import publish_generation
from tests.helpers.research_selection import selected_target

from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
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
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


@pytest.mark.parametrize("recorded", [False, True], ids=["never-recorded", "archived-observation"])
def test_legacy_observation_is_not_a_recomputed_common_share_fact(tmp_path, recorded):
    source_path, app, workspace = tmp_path / "runs", tmp_path / "app", tmp_path / "workspace"
    fixed, _, _ = seed(source_path)
    fixed = fixed.model_copy(deep=True, update={"generation_id": "fixed-financial-evidence"})
    ticker = "1234"
    model = fixed.result("company_financials", ticker)
    cash = {
        **deepcopy(CASH_FLOW),
        "edinet_reported_flows": {
            "consolidation_basis": "nonconsolidated",
            "source_doc_id": "synthetic-edinet",
            "amounts": {
                "operating_cash_flow": -900,
                "free_cash_flow": None,
                "financing_lease_repayments_cash_effect": -12,
                "financing_noncontrolling_dividends_cash_effect": None,
            },
        },
        "edinet_failure_reasons": "metric_unavailable:capex,entity_scale_mismatch",
    }
    model.payload["financial"].update(deepcopy(FORECAST), **cash, book_equity=deepcopy(BOOK))
    model.payload.setdefault("earnings_price", {})["accounting_disclosures"] = [deepcopy(NOTICE)]
    publish_generation(fixed, path=source_path)
    # Archived entries can legitimately be partial, even with a fuller fixed model behind them.
    # A different EPS forces the consumer to use the saved row, not merely agree numerically.
    gate = {"state": "not_selected"}
    paths = {}
    if recorded:
        gate.update(
            forecast_context={
                "forecast_eps_yen": -7.0,
                "earnings_forecast_source": {"disclosure_number": "archived-forecast"},
            },
            cash_flow_context={
                "cfo": -90,
                "statement_basis": "nonconsolidated",
                "period_end": "2026-03-31",
            },
        )
        paths.update(
            book_equity={
                "amount_yen": 12000,
                "measurement_date": "2025-03-31",
                "method": "reported_net_assets",
            },
            accounting_change_context={"disclosures": [{"disclosure_number": "archived-notice"}]},
        )
    row = stored_row(ticker, lane="input_review", gate=gate, paths=paths)
    publication = stored_publication(fixed.generation_id, (row,), order=(ticker,))
    if not recorded:
        publication = replace(
            publication,
            result=replace(
                publication.result,
                policy=TechnicalFinancialPolicy(method="technical-financial-discovery-v5"),
            ),
        )
    sid = "archived-financial-evidence"
    ScreeningSelectionStore(source_path).publish(sid, publication)
    now = read_publication(path=source_path, selection_id=sid).published_at + timedelta(seconds=1)
    original = source_path.read_bytes()
    arguments = args(source_path, now) | {"screening_selection_id": sid}
    listed = load_generated_selection_context(**arguments, screening_lane="input_review")
    assert listed["model_inputs"]["securities"] == {}
    card = listed["screening"]["cards"][0]
    detail = load_generated_selection_context(**arguments, comparison_tickers=(ticker,))
    archive = detail["screening"]["comparison_rows"][0]
    assert archive["financial_gate"] == row.financial_gate
    assert archive["financial_paths"] == row.financial_paths
    for bucket, keys in [
        ("financial_gate", ("forecast_context", "cash_flow_context")),
        ("financial_paths", ("book_equity", "accounting_change_context")),
    ]:
        for key in keys:
            if recorded:
                assert card[bucket][key] == archive[bucket][key]
            else:
                assert key not in card[bucket]
                assert key not in archive[bucket]
    raw = detail["model_inputs"]["securities"][ticker]["models"]["company_financials"]
    assert raw["financial"]["forecast_eps_yen"] == FORECAST["forecast_eps_yen"]
    assert raw["financial"]["earnings_forecast_source"] == FORECAST["earnings_forecast_source"]
    assert raw["financial"]["book_equity"] == BOOK
    assert raw["financial"]["edinet_reported_flows"] == cash["edinet_reported_flows"]
    assert raw["earnings_price"]["accounting_disclosures"] == [NOTICE]
    # The time of the company forecast need not belong to the momentum window.
    momentum = detail["model_inputs"]["securities"][ticker]["models"]["technical_analysis"][
        "price_momentum"
    ]
    assert (
        momentum["end_date"]
        < FORECAST["earnings_forecast_source"]["disclosed_at"]
        < momentum["reference_date"]
    )
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=arguments["request"],
        research_as_of=fixed.as_of,
        input_known_at=now,
        generation_id=fixed.generation_id,
        screening_selection_id=sid,
        selection_summary="旧記録と区分・出典の異なる原資料を照合する",
        targets=(selected_target(ticker),),
    )
    prepared = prepare_selected_workspace(
        draft=draft, db_path=app, workspace=workspace, runs_path=source_path, now=now
    )
    saved = selection_binding(OperationService(app).get(prepared.operation_id).payload)
    projection = project_research_inputs(saved)
    fixed_view = projection["securities"][ticker]["models"]["company_financials"]
    assert {k: v for k, v in fixed_view.items() if k != "full_result_ref"} == {
        k: v for k, v in raw.items() if k != "generation_model_ref"
    }
    assert projection["screening_selection"]["result"]["comparison_rows"] == [archive]
    assert source_path.read_bytes() == original
    source_path.rename(tmp_path / "unavailable-source")
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=app,
        workspace=workspace,
        now=now + timedelta(days=1),
    )
    assert resumed.selection_sha256 == selection_sha256(saved)
    assert not source_path.exists()
