"""見送りを当時の市場根拠で振り返り、後発head/現在progressへ差し替えない。"""

import json
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.engine.investment_judgment.trade_proposal.test_saved_market_context import (
    _market_document,
    _prepare,
)
from tests.helpers.assessment_v1 import assessment_id
from tests.helpers.proposal import PREPARED, PUBLISHED, reviewed_proposal

from baibai_engine.investment_judgment.judgment_retrospective.document import RetrospectiveDraft
from baibai_engine.investment_judgment.judgment_retrospective.query import (
    get_retrospective,
    scaffold_retrospective,
)
from baibai_engine.investment_judgment.judgment_retrospective.store import RetrospectiveStoreService
from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.market_assessment.service import AssessmentService
from baibai_engine.investment_judgment.stock_assessment.store import load_reviewed_assessment
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.workflow.operation.progress import (
    UnfinishedTarget,
    selection_binding,
)
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import saved_market_assessment
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.write import connect_rw

NOW = datetime.fromisoformat("2026-10-20T18:00:00+09:00")


def _case(tmp_path, *, action="no_action", market=True, unfinished=False):
    original = _market_document() if market else None
    app, market_path, runs, operation = _prepare(tmp_path, market_document=original)
    with closing(connect_read_only(app)) as connection:
        pair = load_reviewed_assessment(connection, assessment_id())
    if unfinished:
        operation = OperationService(app).record_outcome(
            operation.operation_id,
            UnfinishedTarget(
                ticker="1234",
                purpose="new",
                unfinished_reason="合成: 個社借換期を未確認",
                references=("https://example.invalid/synthetic-rates",),
            ),
            now=PREPARED,
        )
    case = SimpleNamespace(app=app, market=market_path, operation=operation, pair=pair)
    proposal = TradeProposalService(app, sqlite_path=market_path, clock=lambda: PUBLISHED).publish(
        reviewed_proposal(case, action=action)
    )
    return case, proposal, runs, original


def _scaffold(app, proposal=None):
    return scaffold_retrospective(
        retrospective_id="synthetic-market-retrospective",
        as_of=NOW.date(),
        proposal_id=None if proposal is None else proposal.proposal_id,
        assessment_id=assessment_id() if proposal is None else None,
        ticker=None if proposal is None else "1234",
        milestone="当時の金利/借換懸念を後日資料と比較する",
        db_path=app,
        known_at=NOW,
    )


@pytest.mark.parametrize("action", ["no_action", "defer"])
def test_scaffold_and_revisit_use_exact_market_after_operation_completion(
    tmp_path, monkeypatch, action
):
    case, proposal, runs, original = _case(tmp_path, action=action)
    expected = saved_market_assessment(selection_binding(case.operation.payload))
    operations = OperationService(case.app)
    bound = operations.record_proposal(
        case.operation.operation_id, proposal.proposal_id, now=PUBLISHED
    )
    operations.complete(
        bound.operation_id,
        result="合成判断を保存した",
        next="後日比較",
        completed_at=PUBLISHED + timedelta(minutes=1),
    )
    later = _market_document(suffix="later", published_at=PUBLISHED + timedelta(minutes=2))
    AssessmentService(case.app).publish(later, expected_head=original.context_id)
    runs.rename(tmp_path / "retained-runs.sqlite")

    def forbidden(*_args, **_kwargs):
        pytest.fail("retrospective must not read current market or publication progress")

    monkeypatch.setattr(
        "baibai_engine.investment_judgment.market_assessment.query.latest_assessment", forbidden
    )
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workflow.operation.research_binding.require_proposal_research_scope",
        forbidden,
    )
    before = case.app.read_bytes()
    scaffold = _scaffold(case.app, proposal)
    origin = scaffold["context"]["origin"]
    market = origin["market_context"]
    assert market == {
        "source": "proposal_fixed_selection",
        "selection_sha256": proposal.research_scope.selection_sha256,
        "status": "retained",
        "assessment": expected,
    }
    assert market["assessment"]["known_at"] != market["assessment"]["published_at"]
    assert origin["proposal"]["item"]["action"] == action
    assert case.app.read_bytes() == before
    raw = scaffold["draft"]
    raw["document"]["comparisons"] = [
        {
            "original_claim": "金利高止まりと未確認の個社借換を理由に待つ",
            "observed": "後日資料では個社借換期はまだ確認できない",
            "result": "unresolved",
            "reason": "現在の金利だけでは当時の待機理由を反証できない",
            "citations": [],
        }
    ]
    draft = RetrospectiveDraft.model_validate(raw)
    service = RetrospectiveStoreService(case.app, clock=lambda: NOW)
    saved = service.save(draft)
    before = case.app.read_bytes()
    result = get_retrospective(retrospective_id=saved.reference.retrospective_id, db_path=case.app)
    assert result["record"] == saved.model_dump(mode="json")
    assert result["context"]["origin"] == origin
    assert service.save(draft) == saved
    assert case.app.read_bytes() == before
    market["assessment"]["counter_evidence"].clear()
    assert (
        _scaffold(case.app, proposal)["context"]["origin"]["market_context"]["assessment"]
        == expected
    )


def test_missing_at_selection_is_not_backfilled(tmp_path):
    case, proposal, _runs, _original = _case(tmp_path, market=False)
    AssessmentService(case.app).publish(_market_document(published_at=PREPARED), expected_head=None)
    before = case.app.read_bytes()
    market = _scaffold(case.app, proposal)["context"]["origin"]["market_context"]
    assert market["status"] == "not_recorded"
    assert market["assessment"] is None
    assert case.app.read_bytes() == before


def test_unfinished_defer_keeps_market_without_inventing_an_assessment(tmp_path):
    case, proposal, _runs, _original = _case(tmp_path, action="defer", unfinished=True)
    before = case.app.read_bytes()
    origin = _scaffold(case.app, proposal)["context"]["origin"]
    assert origin["assessment"] is None
    assert origin["proposal"]["item"]["assessment_ref"] is None
    assert origin["market_context"]["assessment"] == saved_market_assessment(
        selection_binding(case.operation.payload)
    )
    assert case.app.read_bytes() == before


def test_old_projection_does_not_invent_unrecorded_cutoff(tmp_path, monkeypatch):
    def old_projection(record, *, as_of):
        result = assessment_projection(record, as_of=as_of)
        result.pop("known_at", None)
        return result

    with monkeypatch.context() as legacy:
        for module in ("workflow.workspace.selected_prepare", "workflow.operation.validation"):
            legacy.setattr(
                f"baibai_engine.investment_judgment.{module}.assessment_projection", old_projection
            )
        case, proposal, _runs, _original = _case(tmp_path)
    market = _scaffold(case.app, proposal)["context"]["origin"]["market_context"]
    assert market["status"] == "retained"
    assert "known_at" not in market["assessment"]
    assert market["assessment"] == saved_market_assessment(
        selection_binding(case.operation.payload)
    )


@pytest.mark.parametrize("missing", ["operation", "selection", "unreadable", "hash_mismatch"])
def test_unavailable_old_selection_is_explicit_without_breaking_original_pair(tmp_path, missing):
    case, proposal, _runs, _original = _case(tmp_path)
    with closing(connect_rw(case.app)) as connection, connection:
        if missing == "operation":
            connection.execute(
                "DELETE FROM operation_session WHERE operation_id=?", (case.operation.operation_id,)
            )
        else:
            payload = deepcopy(case.operation.payload.model_dump(mode="json"))
            if missing == "selection":
                payload["artifacts"] = [
                    item
                    for item in payload["artifacts"]
                    if item.get("kind") != "research_selection"
                ]
            elif missing == "unreadable":
                payload["artifacts"] = "not-an-artifact-list"
            else:
                for item in payload["artifacts"]:
                    if item.get("kind") == "research_selection":
                        item["selection_summary"] = "合成破損対照:異なる選定"
            connection.execute(
                "UPDATE operation_session SET payload=? WHERE operation_id=?",
                (json.dumps(payload), case.operation.operation_id),
            )
    before = case.app.read_bytes()
    origin = _scaffold(case.app, proposal)["context"]["origin"]
    assert origin["market_context"]["status"] == "unavailable"
    assert origin["market_context"]["assessment"] is None
    assert (
        origin["market_context"]["reason"]
        == {
            "operation": "operation_not_found",
            "selection": "selection_not_recorded",
            "unreadable": "selection_unreadable",
            "hash_mismatch": "selection_hash_mismatch",
        }[missing]
    )
    assert origin["proposal"]["item"] == proposal.items[0].model_dump(mode="json")
    assert origin["assessment"]["document"] == case.pair.document.model_dump(mode="json")
    assert case.app.read_bytes() == before


def test_assessment_only_does_not_infer_market_from_other_proposals(tmp_path):
    case, _proposal, _runs, _original = _case(tmp_path)
    before = case.app.read_bytes()
    origin = _scaffold(case.app)["context"]["origin"]
    assert origin["proposal"] is None
    assert origin["market_context"] is None
    assert case.app.read_bytes() == before
