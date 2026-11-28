"""選定時の問いを最終比較へ戻す。未回答や元のリスクを新しい採否へ変換しない。"""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from tests.helpers import proposal as helper
from tests.helpers.db_seed import seed_historical_operation
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.proposal import PROPOSAL_ID
from tests.helpers.research_selection import historical_operation, selection

from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import parse_selection, selection_sha256

START = helper.PREPARED - timedelta(minutes=3)
REQUEST = (
    "合成の元依頼：回復候補と現金維持を比べてください。\n"
    "反証が残る場合は価格待ちと情報待ちを分け、買付を前提にしない。"
    + "元依頼だけに記された比較上の限定。"
    * 30
)


def _selected(tmp_path, *, tickers=("1234",), scope="discover_new"):
    source = seed_generated_selection(
        runs=tmp_path / "runs.sqlite", now=START, tickers=tickers, scope=scope
    )
    return source.model_copy(
        update={
            "request": source.request.model_copy(update={"text": REQUEST}),
            "selection_summary": "合成：合致数でなく、回答が採否を変える問いを優先する。",
            "targets": tuple(
                item.model_copy(
                    update={
                        "rationale": "営業回復を確認したが、持続利益とは未認定。"
                        + "比較材料。" * 100,
                        "research_question": "費用低下が契約更新後も残るなら価値仮説を検討できるか。",
                        "key_risk": "為替と税調整を恒常利益へ外挿しない。",
                    }
                )
                for item in source.targets
            ),
        }
    )


def _finish_research(operations, operation):
    for target in selection_binding(operation.payload).targets:
        operations.record_unfinished(
            operation.operation_id,
            ticker=target.ticker,
            reason="選定時の問いが未確認。企業価値の棄却とは別。",
            references=("synthetic-open-question",),
            now=helper.PREPARED,
        )


def _expected(selected):
    return {
        "role": "selection_time_rationale_not_final_judgment",
        "selection_sha256": selection_sha256(selected),
        "research_as_of": selected.research_as_of.isoformat(),
        "input_known_at": selected.input_known_at.isoformat(),
        "request": selected.request.model_dump(mode="json"),
        "selection_summary": selected.selection_summary,
        "targets": [item.model_dump(mode="json") for item in selected.targets],
    }


@pytest.mark.parametrize("scope", ["discover_new", "specified_stocks"])
def test_normal_operation_keeps_original_questions_after_source_is_unavailable(
    tmp_path, monkeypatch, scope
):
    app = tmp_path / "app.sqlite"
    helper.seed_context_ledger(app, START)
    selected = _selected(tmp_path, scope=scope)
    assert REQUEST not in selected.selection_summary
    assert all(REQUEST not in target.research_question for target in selected.targets)
    operations = OperationService(app, runs_path=tmp_path / "runs.sqlite")
    operation = operations.start(
        session_kind="capital-allocation",
        as_of=selected.research_as_of,
        started_at=START,
        payload=OperationPayload(
            checkpoint="research", artifacts=(selected.model_dump(mode="json"),)
        ),
    )
    _finish_research(operations, operation)
    (tmp_path / "runs.sqlite").rename(tmp_path / "retained-runs.sqlite")

    def forbidden(*_args, **_kwargs):
        pytest.fail("final selection context must not reread current model or Research heads")

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.generation.query.read_research_model_inputs", forbidden
    )
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.company_research.query.read_company_contexts", forbidden
    )
    before = app.read_bytes()
    context = build_proposal_context(
        proposal_id=PROPOSAL_ID,
        operation_id=operation.operation_id,
        db_path=app,
        sqlite_path=tmp_path / "absent-market.sqlite",
        now=helper.PREPARED,
    )
    payload = context.payload()
    assert payload["saved_selection_context"] == _expected(selected)
    assert payload["research_scope"]["selection_sha256"] == selection_sha256(selected)
    assert payload["research_scope"]["outcomes"][0]["status"] == "unfinished"
    assert payload["assessments"] == payload["calculations"] == {}
    assert payload["saved_selection_context"]["request"]["text"] == REQUEST
    payload["saved_selection_context"]["request"]["text"] = "返却値だけの別依頼"
    payload["saved_selection_context"]["request"]["requested_tickers"].append("9999")
    payload["saved_selection_context"]["targets"][0]["research_question"] = "返却値の変更"
    assert context.payload()["saved_selection_context"] == _expected(selected)
    assert selection_binding(operations.get(operation.operation_id).payload) == selected
    assert app.read_bytes() == before


@pytest.mark.parametrize("version", [2, 3, 4, 5])
def test_retained_selection_versions_keep_original_text_and_cutoff(tmp_path, version):
    app = tmp_path / "app.sqlite"
    helper.seed_context_ledger(app, START)
    if version == 2:
        selected = selection(now=START, tickers=("1234",))
    else:
        selected = _selected(tmp_path)
        if version != 5:
            from tests.helpers.generated_selection import legacy_selection_payload

            selected = parse_selection(
                legacy_selection_payload(selected, tmp_path / "runs.sqlite", version=version)
            )
    operation = historical_operation(selected)
    seed_historical_operation(app, operation)
    operations = OperationService(app)
    _finish_research(operations, operation)
    before = app.read_bytes()
    payload = build_proposal_context(
        proposal_id=PROPOSAL_ID,
        operation_id=operation.operation_id,
        db_path=app,
        sqlite_path=tmp_path / "absent-market.sqlite",
        now=helper.PREPARED,
    ).payload()
    assert payload["saved_selection_context"] == _expected(selected)
    assert payload["saved_selection_context"]["input_known_at"] != payload["inputs"]["checked_at"]
    assert selection_binding(operations.get(operation.operation_id).payload) == selected
    assert app.read_bytes() == before


def test_zero_target_still_does_not_enter_proposal_authoring(tmp_path):
    app = tmp_path / "app.sqlite"
    helper.seed_context_ledger(app, START)
    selected = _selected(tmp_path, tickers=())
    operations = OperationService(app, runs_path=tmp_path / "runs.sqlite")
    operation = operations.start(
        session_kind="capital-allocation",
        as_of=selected.research_as_of,
        started_at=START,
        payload=OperationPayload(
            checkpoint="research", artifacts=(selected.model_dump(mode="json"),)
        ),
    )
    before = app.read_bytes()
    with pytest.raises(ValidationError, match="at least 1 item"):
        build_proposal_context(
            proposal_id=PROPOSAL_ID,
            operation_id=operation.operation_id,
            db_path=app,
            sqlite_path=tmp_path / "absent-market.sqlite",
            now=helper.PREPARED,
        )
    assert selection_binding(operations.get(operation.operation_id).payload) == selected
    assert app.read_bytes() == before


@pytest.mark.parametrize("holding", [False, True])
def test_selection_questions_do_not_change_reviewed_action_or_exact_retry(tmp_path, holding):
    case = helper.backend_case(tmp_path, holding=holding)
    action = "hold" if holding else "buy"
    draft = helper.reviewed_proposal(case, action=action)
    service = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: helper.PUBLISHED,
    )
    assert service.check(draft).valid
    saved = service.publish(draft)
    before = case.app.read_bytes()
    context = build_proposal_context(
        proposal_id=PROPOSAL_ID,
        operation_id=case.operation.operation_id,
        db_path=case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        now=helper.PREPARED,
    ).payload()
    assert context["saved_selection_context"] == _expected(case.selected)
    assert context["saved_selection_context"]["targets"][0]["purpose"] == (
        "holding" if holding else "new"
    )
    assert service.publish(draft) == saved
    assert helper.read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert case.app.read_bytes() == before
