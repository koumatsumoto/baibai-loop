"""選定時の機械根拠を最終判断と照合する。候補合致を採否へ昇格しない。"""

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import TypeAdapter
from tests.helpers import proposal as helper
from tests.helpers.db_seed import seed_historical_operation, seed_ledger
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.proposal import PROPOSAL_ID
from tests.helpers.research_selection import historical_operation, selected_target, selection

from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    parse_selection,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.workspace import prepare_selected_workspace
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialRow,
    TechnicalFinancialSelection,
)
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument

START = helper.PREPARED - timedelta(minutes=3)
CODEC = TypeAdapter(TechnicalFinancialRow)


def _result(version=12):
    rows = (
        TechnicalFinancialRow(
            "1234",
            "合成回復企業",
            "合成業種",
            {
                "state": "unavailable",
                "matched_routes": [],
                "missing_models": ["technical_analysis"],
            },
            {
                "state": "research_candidate",
                "matched_routes": ["operating_recovery"],
                "cash_flow_context": {"operating_cash_flow": -10, "operating_cash_flow_ttm": None},
                "forecast_context": {"forecast_profit": -1, "forecast_loss_flag": True},
                "checks": {"profit": {"value": 0, "matched": False}},
            },
            {
                "matched_routes": [],
                "paths": [
                    {
                        "path_id": "book",
                        "eligible": False,
                        "reason": "common_book_not_observed",
                        "price_value": None,
                    }
                ],
            },
            "growth_research",
        ),
        TechnicalFinancialRow(
            "2345",
            "合成未確認企業",
            None,
            {"state": "matched", "matched_routes": ["rebound_start"]},
            {
                "state": "input_unresolved",
                "matched_routes": [],
                "missing_models": ["company_growth"],
            },
            {"matched_routes": [], "paths": [], "not_generated": ["company_growth"]},
            "input_review",
        ),
        TechnicalFinancialRow(
            "3456",
            "合成候補外企業",
            None,
            {"state": "matched", "matched_routes": ["rebound_start"]},
            {"state": "not_selected", "matched_routes": [], "reason": "known_failure"},
            {"matched_routes": [], "paths": []},
            "no_current_hypothesis",
        ),
    )
    if version < 6:
        rows[0].financial_gate.pop("forecast_context")
        rows[0].financial_gate.pop("cash_flow_context")
    return TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method=f"technical-financial-discovery-v{version}"),
        "retained-technical",
        "retained-financial",
        rows,
        ("2345", "1234"),
        {"planned_stocks": 3},
    )


def _ledger(app):
    seed_ledger(
        app,
        PortfolioLedgerDocument.model_validate(
            {
                "schema_version": 3,
                "portfolio_scope": "repository_only",
                "as_of": START,
                "events": [
                    {
                        "type": "opening_balance",
                        "event_id": "synthetic",
                        "occurred_at": START,
                        "amount_yen": 1,
                    }
                ],
            }
        ),
    )


def _prepare(tmp_path, *, tickers=("1234",), version=12, screening=True, technical=True):
    app, runs = tmp_path / "app.sqlite", tmp_path / "runs.sqlite"
    _ledger(app)
    source = seed_generated_selection(
        runs=runs,
        now=START,
        tickers=("1234", "2345", "3456"),
        screening_result=_result(version) if technical else None,
    )
    prepared = prepare_selected_workspace(
        draft=GeneratedResearchSelectionDraft(
            schema_version=2,
            request=ResearchRequest(
                text="合成比較", scope="specified_stocks", requested_tickers=tickers
            ),
            research_as_of=source.research_as_of,
            input_known_at=source.input_known_at,
            generation_id=source.inputs.generation.generation_id,
            screening_selection_id=source.inputs.screening_selection_id if screening else None,
            selection_summary="支持と不利を問いへ結ぶ。候補合致を採択にしない。",
            targets=tuple(selected_target(t) for t in tickers),
        ),
        db_path=app,
        runs_path=runs,
        workspace=tmp_path / "workspace",
        now=START,
    )
    operations = OperationService(app)
    for ticker in tickers:
        operations.record_unfinished(
            prepared.operation_id,
            ticker=ticker,
            reason="問いが未完",
            references=("synthetic-open",),
            now=helper.PREPARED,
        )
    saved = selection_binding(operations.get(prepared.operation_id).payload)
    return app, runs, prepared.operation_id, saved


def _read(app, operation_id, tmp_path):
    return build_proposal_context(
        proposal_id=PROPOSAL_ID,
        operation_id=operation_id,
        db_path=app,
        sqlite_path=tmp_path / "absent-market",
        now=helper.PREPARED,
    )


@pytest.mark.parametrize("ticker", ["1234", "3456"])
def test_final_context_keeps_selected_row_and_counterevidence_without_source(
    tmp_path, monkeypatch, ticker
):
    app, runs, operation_id, saved = _prepare(tmp_path, tickers=(ticker,))
    runs.rename(tmp_path / "retained-runs.sqlite")

    def forbidden(*_args, **_kwargs):
        pytest.fail("discovery context must use fixed rows, not current models or policy")

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.generation.query.read_research_model_inputs", forbidden
    )
    before = app.read_bytes()
    context = _read(app, operation_id, tmp_path)
    payload = context.payload()
    view = payload["saved_discovery_context"]
    assert view["role"] == "selection_time_discovery_evidence_not_final_judgment"
    assert view["selection_sha256"] == selection_sha256(saved)
    assert view["screening_selection_id"] == saved.inputs.screening_selection_id
    assert view["generation_id"] == saved.inputs.generation.generation_id
    assert view["input_known_at"] == saved.input_known_at.isoformat()
    assert view["research_as_of"] == saved.research_as_of.isoformat()
    assert view["published_at"] == saved.inputs.screening_published_at.isoformat()
    assert view["policy"] == saved.inputs.screening_selection.result.policy.model_dump(mode="json")
    assert view["rows"] == [
        CODEC.dump_python(row, mode="json")
        for row in saved.inputs.screening_selection.result.rows
        if row.ticker == ticker
    ]
    assert view["not_in_saved_rows"] == []
    assert "display_order" not in view
    assert "summary" not in view
    assert payload["assessments"] == payload["calculations"] == {}
    assert payload["research_scope"]["outcomes"][0]["status"] == "unfinished"
    view["rows"][0]["financial_gate"].clear()
    assert context.payload()["saved_discovery_context"]["rows"][0]["financial_gate"]
    assert app.read_bytes() == before
    assert selection_binding(OperationService(app).get(operation_id).payload) == saved


@pytest.mark.parametrize("version", [1, 11, 12])
def test_original_policy_and_absent_fields_are_not_backfilled(tmp_path, version):
    app, _, operation_id, saved = _prepare(tmp_path, version=version)
    before = saved.model_dump_json()
    view = _read(app, operation_id, tmp_path).payload()["saved_discovery_context"]
    assert view["policy"]["method"] == f"technical-financial-discovery-v{version}"
    assert view["technical_policy_id"] == "retained-technical"
    assert view["financial_policy_id"] == "retained-financial"
    assert ("forecast_context" in view["rows"][0]["financial_gate"]) is (version >= 6)
    assert saved.model_dump_json() == before


def test_only_selected_targets_are_returned_in_request_order(tmp_path):
    app, _, operation_id, _ = _prepare(tmp_path, tickers=("3456", "1234", "2345"))
    view = _read(app, operation_id, tmp_path).payload()["saved_discovery_context"]
    assert [row["ticker"] for row in view["rows"]] == ["3456", "1234", "2345"]
    assert view["rows"][0]["screening_lane"] == "no_current_hypothesis"
    assert view["rows"][2]["financial_gate"]["state"] == "input_unresolved"


def test_explicit_target_outside_saved_rows_is_not_given_a_candidate_state(tmp_path):
    app, _, operation_id, _ = _prepare(tmp_path, tickers=("9999",))
    view = _read(app, operation_id, tmp_path).payload()["saved_discovery_context"]
    assert view["rows"] == []
    assert view["not_in_saved_rows"] == ["9999"]


def test_generation_without_screening_is_not_recomputed(tmp_path):
    app, _, operation_id, saved = _prepare(tmp_path, screening=False)
    assert saved.inputs.screening_selection is None
    assert _read(app, operation_id, tmp_path).payload()["saved_discovery_context"] is None


def test_other_saved_candidate_policy_is_not_reinterpreted(tmp_path):
    app, _, operation_id, saved = _prepare(tmp_path, technical=False)
    before = saved.model_dump_json()
    assert _read(app, operation_id, tmp_path).payload()["saved_discovery_context"] is None
    assert (
        selection_binding(OperationService(app).get(operation_id).payload).model_dump_json()
        == before
    )


@pytest.mark.parametrize("version", [2, 3])
def test_old_selection_remains_readable_without_new_source_or_hash(tmp_path, version):
    app = tmp_path / "legacy.sqlite"
    _ledger(app)
    if version == 2:
        selected = selection(now=START, tickers=("1234",))
    else:
        selected = seed_generated_selection(
            runs=tmp_path / "runs",
            now=START,
            tickers=("1234", "2345", "3456"),
            screening_result=_result(1),
        )
        from tests.helpers.generated_selection import legacy_selection_payload

        raw = legacy_selection_payload(selected, tmp_path / "runs", version=3)
        selected = parse_selection(raw)
    operation = historical_operation(selected)
    seed_historical_operation(app, operation)
    operations = OperationService(app)
    for target in selected.targets:
        operations.record_unfinished(
            operation.operation_id,
            ticker=target.ticker,
            reason="旧保存未完",
            references=("legacy",),
            now=helper.PREPARED,
        )
    before = app.read_bytes()
    view = _read(app, operation.operation_id, tmp_path).payload()["saved_discovery_context"]
    assert (view is None) is (version == 2)
    if view is not None:
        assert view["rows"][0] == CODEC.dump_python(_result(1).rows[0], mode="json")
    assert app.read_bytes() == before
    assert selection_binding(operations.get(operation.operation_id).payload) == selected


@pytest.mark.parametrize("holding", [False, True])
def test_discovery_nonmatch_does_not_change_reviewed_action_or_exact_retry(
    tmp_path, monkeypatch, holding
):
    original = helper.seed_generated_selection

    def selected(**kwargs):
        result = _result()
        only = result.rows[2]
        only = replace(only, ticker="1234")
        result = replace(result, rows=(only,), display_order=())
        return original(**kwargs, screening_result=result)

    monkeypatch.setattr(helper, "seed_generated_selection", selected)
    case = helper.backend_case(tmp_path, holding=holding)
    draft = helper.reviewed_proposal(case, action="hold" if holding else "buy")
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: helper.PUBLISHED
    )
    assert service.check(draft).valid
    saved = service.publish(draft)
    before = case.app.read_bytes()
    context = build_proposal_context(
        proposal_id=PROPOSAL_ID,
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=helper.PREPARED,
    ).payload()
    assert (
        context["saved_discovery_context"]["rows"][0]["screening_lane"] == "no_current_hypothesis"
    )
    assert service.publish(draft) == saved
    assert helper.read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert case.app.read_bytes() == before
