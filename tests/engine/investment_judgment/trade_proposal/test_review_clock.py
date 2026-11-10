from __future__ import annotations

from dataclasses import replace
from datetime import UTC, timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import (
    PREPARED,
    PUBLISHED,
    backend_case,
    read_saved_proposal,
    reviewed_proposal,
)

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal import service as proposal_service
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalError,
    proposal_draft_sha256,
)
from baibai_engine.portfolio.ledger.store import LedgerStoreService


def _case_with_pair_times(tmp_path, *, assessed_at, reviewed_at):
    case = backend_case(tmp_path)
    document, review = pair_payload(review_number=9)
    document["conclusion"]["assessed_at"] = assessed_at.isoformat()
    review["reviewed_at"] = reviewed_at.isoformat()
    update_review_hash(document, review)
    case.pair = AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED - timedelta(minutes=2)
    ).publish_reviewed_assessment(
        assessment_id(9), document, review, supersedes_id=case.pair.assessment_id
    )
    case.operations.record_assessment(
        case.operation.operation_id, case.pair.assessment_id, now=PUBLISHED - timedelta(minutes=1)
    )
    return case


@pytest.mark.parametrize(
    ("action", "assessed_at"),
    [
        ("buy", PREPARED + timedelta(minutes=6)),
        ("no_action", PREPARED - timedelta(minutes=10)),
        ("defer", PREPARED - timedelta(minutes=10)),
    ],
)
def test_new_proposal_review_cannot_precede_its_exact_assessment_review(
    tmp_path, action, assessed_at
):
    case = _case_with_pair_times(
        tmp_path, assessed_at=assessed_at, reviewed_at=PREPARED + timedelta(minutes=7)
    )
    proposal = reviewed_proposal(case, action=action)
    assert proposal.review.reviewed_at < case.pair.review.reviewed_at < PUBLISHED
    service = proposal_service.TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    )
    before = case.app.read_bytes()
    for require_review in (False, True):
        validation = service.check(proposal, require_review=require_review)
        assert not validation.valid
        assert [reason.code for reason in validation.blockers] == ["assessment_after_review"]
    with pytest.raises(ProposalError, match="参照評価の独立Review"):
        service.publish(proposal)
    assert case.app.read_bytes() == before


@pytest.mark.parametrize(
    ("assessed_at", "reviewed_at"),
    [
        (PREPARED - timedelta(minutes=10), PREPARED - timedelta(minutes=5)),
        (PREPARED + timedelta(minutes=2), (PREPARED + timedelta(minutes=5)).astimezone(UTC)),
    ],
)
def test_completed_assessment_may_be_saved_after_proposal_review(
    tmp_path, assessed_at, reviewed_at
):
    case = _case_with_pair_times(tmp_path, assessed_at=assessed_at, reviewed_at=reviewed_at)
    proposal = reviewed_proposal(case)
    assert case.pair.review.reviewed_at <= proposal.review.reviewed_at < case.pair.published_at
    ledger = LedgerStoreService(case.app).load()
    service = proposal_service.TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    )
    assert service.check(proposal).valid
    saved = service.publish(proposal)
    assert read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == saved
    assert LedgerStoreService(case.app).load() == ledger


def test_saved_proposal_predating_assessment_review_keeps_hash_readback_and_retry(
    tmp_path, monkeypatch
):
    case = _case_with_pair_times(
        tmp_path,
        assessed_at=PREPARED + timedelta(minutes=6),
        reviewed_at=PREPARED + timedelta(minutes=7),
    )
    proposal = reviewed_proposal(case)
    service = proposal_service.TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    )
    check = proposal_service.check_proposal

    def legacy_check(*args, **kwargs):
        # 旧writerが受理した本文を再現する。今回の時刻検証以外は全て通す。
        validation = check(*args, **kwargs)
        assert [reason.code for reason in validation.blockers] == ["assessment_after_review"]
        return replace(validation, valid=True, blockers=())

    with monkeypatch.context() as old_writer:
        old_writer.setattr(proposal_service, "check_proposal", legacy_check)
        saved = service.publish(proposal)
    before = case.app.read_bytes()
    digest = proposal_draft_sha256(proposal)
    assert not service.check(proposal).valid
    assert read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == saved
    retry = proposal_service.TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PREPARED
    ).publish(proposal)
    assert retry == saved
    assert retry.review.draft_sha256 == proposal_draft_sha256(retry) == digest
    assert case.app.read_bytes() == before
