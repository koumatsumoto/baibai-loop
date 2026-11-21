"""Synthetic fixtures for preserved proposals and their separate correction/report history."""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    TradeProposal,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReportDraft,
    ProposalCorrectionDraft,
    ProposalReference,
)
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal

SECOND_PROPOSAL_ID = "investment-proposal-00000000-0000-4000-8000-000000000004"


def ref(p: TradeProposal) -> ProposalReference:
    return ProposalReference(proposal_id=p.proposal_id, draft_sha256=proposal_draft_sha256(p))


def snapshot(path: Path):
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
def records_case(tmp_path: Path):
    case = backend_case(tmp_path)
    original = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case))
    wrong = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish(reviewed_proposal(case, action="no_action", proposal_id=SECOND_PROPOSAL_ID))
    case.operations.complete(
        case.operation.operation_id,
        proposal_id=wrong.proposal_id,
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


def not_purchased_report(
    proposal: TradeProposal,
    *,
    identity="synthetic-report",
    reported_at: datetime = PUBLISHED + timedelta(minutes=10),
) -> HumanProposalReportDraft:
    return HumanProposalReportDraft(
        report_id=identity,
        proposal_ref=ref(proposal),
        outcome="not_purchased",
        statement="今回は購入しなかった。",
        reason_quote="予定支出に備えて現金を残した。",
        context_summary="担当者による合成の文脈要約。企業価値を否定したとは扱わない。",
        reported_at=reported_at,
        subject_at=None,
    )
