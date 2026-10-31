"""Proposalの訂正・人間報告は金融判断/取引の書換えではない。全入力は架空。"""

import json
import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.cli import investment_judgment
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalConflictError,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.query import (
    load_latest_investment_proposal,
    preview_current_terms,
)
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReportDraft,
    ProposalCorrectionDraft,
    ProposalReference,
)
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.sources.proposal import DbProposalSource

B = "investment-proposal-00000000-0000-4000-8000-000000000004"
C = "investment-proposal-00000000-0000-4000-8000-000000000005"


def ref(p):
    return ProposalReference(proposal_id=p.proposal_id, draft_sha256=proposal_draft_sha256(p))


def snapshot(path):
    with closing(sqlite3.connect(path)) as c:
        return {
            n: c.execute('SELECT * FROM "' + n + '"').fetchall()
            for n in [
                "investment_proposal",
                "investment_proposal_item",
                "stock_assessment",
                "stock_assessment_review",
                "operation_session",
                "ledger_event",
                "ledger_meta",
                "position_decision_reference",
            ]
        }


@pytest.fixture
def records_case(tmp_path):
    case = backend_case(tmp_path)
    original = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case))
    wrong = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish(reviewed_proposal(case, action="no_action", proposal_id=B))
    case.operations.record_proposal(
        case.operation.operation_id, wrong.proposal_id, now=PUBLISHED + timedelta(minutes=2)
    )
    case.operations.complete(
        case.operation.operation_id,
        result="合成の有限判断完了",
        next="人間の判断と実取引は別",
        completed_at=PUBLISHED + timedelta(minutes=2),
    )
    service = ProposalRecordService(case.app, clock=lambda: PUBLISHED + timedelta(hours=1))
    correction = ProposalCorrectionDraft(
        correction_id="synthetic-correction",
        erroneous_ref=ref(wrong),
        replacement_ref=ref(original),
        reason="合成の担当者が人間の非購入報告を投資判断へ誤変換したため。",
    )
    return case, original, wrong, service, correction


def test_correction_report_serving_and_preview_preserve_originals_and_ledger(records_case):
    case, a, b, service, draft = records_case
    before = snapshot(case.app)
    corrected = service.correct(draft, expected_latest_ref=ref(b))
    assert service.correct(draft, expected_latest_ref=ref(b)) == corrected
    report = HumanProposalReportDraft(
        report_id="synthetic-report",
        proposal_ref=ref(a),
        outcome="not_purchased",
        statement="今回は購入しなかった。",
        reported_at=PUBLISHED + timedelta(minutes=10),
        reason_quote="今回は待つ。",
        context_summary="担当者による合成の文脈要約。",
    )
    saved = service.report(report)
    assert service.report(report) == saved
    assert saved.subject_at is None
    assert snapshot(case.app) == before
    with closing(connect_read_only(case.app)) as c:
        assert load_latest_investment_proposal(c).proposal_id == a.proposal_id
    view = DbProposalSource(case.app).latest()
    assert view.proposal_id == a.proposal_id
    assert view.published_at == a.published_at
    assert view.items[0].terms.limit_price_yen == "1000"
    assert view.corrections[0] == corrected
    assert view.human_reports[0] == saved
    preview = preview_current_terms(
        proposal_id=a.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(days=1),
    )
    assert preview.status == "blocked"
    assert any(x.code == "valuation_basis_requires_refresh" for x in preview.change_reasons)
    assert preview.records["human_reports"][0]["outcome"] == "not_purchased"
    assert snapshot(case.app) == before


def test_later_unrelated_proposal_remains_latest(records_case):
    case, _a, b, service, draft = records_case
    # Add an otherwise exact immutable synthetic header/payload at a later instant;
    # normal proposal publication of the closed Operation must remain forbidden.
    from baibai_engine.investment_judgment.trade_proposal.contracts import TradeProposal

    raw = b.model_dump(mode="json")
    raw["proposal_id"] = C
    raw["published_at"] = (PUBLISHED + timedelta(minutes=3)).isoformat()
    from baibai_engine.investment_judgment.trade_proposal.contracts import load_proposal_draft

    raw["review"]["draft_sha256"] = proposal_draft_sha256(load_proposal_draft(raw))
    p = TradeProposal.model_validate(raw)
    with closing(sqlite3.connect(case.app)) as c, c:
        c.execute(
            "INSERT INTO investment_proposal VALUES (?,?,?,?,?,?,?)",
            (
                C,
                str(p.as_of),
                p.prepared_at.isoformat(),
                p.published_at.isoformat(),
                p.request_scope,
                json.dumps(p.payload()),
                proposal_draft_sha256(p),
            ),
        )
        for index, item in enumerate(p.items):
            c.execute(
                "INSERT INTO investment_proposal_item VALUES (?,?,?,?,?)",
                (C, index, item.ticker, item.action, item.assessment_ref.assessment_id),
            )
    with pytest.raises(ProposalConflictError, match="latest changed"):
        service.correct(draft, expected_latest_ref=ref(b))
    service.correct(draft, expected_latest_ref=ref(p))
    assert DbProposalSource(case.app).latest().proposal_id == C


def test_exact_refs_duplicates_cycles_chains_and_immutable(records_case):
    case, a, b, service, draft = records_case
    bad = draft.model_copy(
        update={"erroneous_ref": ref(b).model_copy(update={"draft_sha256": "0" * 64})}
    )
    with pytest.raises(ProposalConflictError, match="exact reference"):
        service.correct(bad, expected_latest_ref=ref(b))
    reverse = draft.model_copy(update={"erroneous_ref": ref(a), "replacement_ref": ref(b)})
    with pytest.raises(ProposalConflictError, match="earlier"):
        service.correct(reverse, expected_latest_ref=ref(b))
    service.correct(draft, expected_latest_ref=ref(b))
    with pytest.raises((ProposalConflictError, sqlite3.IntegrityError)):
        service.correct(
            draft.model_copy(update={"correction_id": "second"}), expected_latest_ref=ref(a)
        )
    with pytest.raises(ProposalConflictError, match="immutable"):
        service.correct(
            draft.model_copy(update={"reason": "different"}), expected_latest_ref=ref(a)
        )
    with closing(sqlite3.connect(case.app)) as c:
        for query in [
            "UPDATE proposal_correction SET payload='{}'",
            "DELETE FROM proposal_correction",
        ]:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                c.execute(query)
        assert c.execute("SELECT COUNT(*) FROM proposal_correction").fetchone()[0] == 1


def test_cli_report_roundtrip_and_conflicting_report(records_case, tmp_path, capsys):
    case, a, b, service, _draft = records_case
    report = HumanProposalReportDraft(
        report_id="report-cli",
        proposal_ref=ref(a),
        outcome="not_purchased",
        statement="購入しなかった。",
        reported_at=PUBLISHED + timedelta(minutes=5),
    )
    path = tmp_path / "report.json"
    path.write_text(report.model_dump_json())
    assert (
        investment_judgment.main(["proposal-records", "--db", str(case.app), "report", str(path)])
        == 0
    )
    assert (
        investment_judgment.main(["proposal-records", "--db", str(case.app), "show", a.proposal_id])
        == 0
    )
    assert "not_purchased" in capsys.readouterr().out
    with pytest.raises(ProposalConflictError, match="immutable"):
        service.report(report.model_copy(update={"statement": "changed"}))

    with pytest.raises(ProposalConflictError, match="immutable"):
        service.report(report.model_copy(update={"proposal_ref": ref(b)}))
