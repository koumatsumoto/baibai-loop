"""新規候補の前回案。保存条件・本人の言葉は当時の説明で、現在の経済入力ではない。"""

import sqlite3
from contextlib import closing
from datetime import timedelta

from tests.helpers.proposal import (
    PREPARED,
    PROPOSAL_ID,
    PUBLISHED,
    backend_case,
    bind_review,
    reviewed_proposal,
)

from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReportDraft,
    ProposalReference,
)
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService
from baibai_engine.investment_judgment.trade_proposal.scaffold import scaffold_proposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService


def _context(case, *, now=PUBLISHED, proposal_id=None):
    return build_proposal_context(
        operation_id=case.operation.operation_id,
        proposal_id=proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=now,
    )


def test_previous_buy_is_the_exact_target_row_not_current_terms_or_an_execution(tmp_path):
    case = backend_case(tmp_path, tickers=("1234", "5678"))
    case.operations.record_unfinished(
        case.operation.operation_id,
        ticker="5678",
        reason="合成の追加資料を待つ",
        references=("synthetic",),
        now=PUBLISHED - timedelta(minutes=11),
    )
    receipt = PUBLISHED + timedelta(minutes=3)
    before_publication = _context(case, now=receipt)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case))
    ref = ProposalReference(proposal_id=saved.proposal_id, draft_sha256=saved.review.draft_sha256)
    report = ProposalRecordService(case.app, clock=lambda: receipt).report(
        HumanProposalReportDraft(
            report_id="synthetic-c24",
            proposal_ref=ref,
            outcome="not_purchased",
            statement="合成：このとき出た提案では購入しなかった。",
            reported_at=PUBLISHED + timedelta(minutes=2),
            subject_at=PUBLISHED + timedelta(minutes=1),
        )
    )
    app_before, market_before = case.app.read_bytes(), case.market.read_bytes()
    excluded = _context(case, now=receipt, proposal_id=saved.proposal_id).previous_buy_context
    assert excluded["targets"]["1234"]["status"] == "not_found"
    just_before = _context(case, now=receipt - timedelta(microseconds=1)).previous_buy_context
    assert just_before["targets"]["1234"]["human_reports"] == []
    context = _context(case, now=receipt)
    historic = context.payload()["previous_buy_context"]
    assert historic["recorded_through"] == receipt.isoformat()
    assert set(historic["targets"]) == {"1234", "5678"}
    assert historic["targets"]["5678"]["status"] == "not_found"
    row = historic["targets"]["1234"]
    [item] = [i for i in saved.items if i.action == "buy"]
    assert row["proposal_ref"] == ref.model_dump(mode="json")
    assert saved.items[row["item_index"]] == item
    assert row["proposal_item_count"] == 2
    assert row["assessment_ref"] == item.assessment_ref.model_dump(mode="json")
    assert row["as_of"] == saved.as_of.isoformat()
    assert row["published_at"] == saved.published_at.isoformat()
    assert row["previous_timing"] == item.timing
    assert row["previous_terms"] == {
        "target_session": item.terms.target_session.isoformat(),
        "limit_price_yen": "1000",
        "quantity": 300,
        "conditions": list(item.terms.conditions),
    }
    [previous_quote] = [q for q in saved.inputs.quotes if q.ticker == item.ticker]
    assert row["previous_quote"] == previous_quote.model_dump(mode="json")
    assert row["previous_quote"]["price_yen"] != row["previous_terms"]["limit_price_yen"]
    assert row["previous_rationale"] == item.rationale
    assert row["previous_review_conditions"] == list(item.review_conditions)
    assert row["human_report_scope"] == "whole_proposal"
    assert row["human_reports"] == [report.model_dump(mode="json")]
    assert context.inputs == before_publication.inputs
    assert context.calculations == before_publication.calculations
    assert not context.inputs.positions
    assert not context.inputs.active_reservations
    row["human_reports"].clear()
    assert context.previous_buy_context["targets"]["1234"]["human_reports"]
    assert (case.app.read_bytes(), case.market.read_bytes()) == (app_before, market_before)


def test_latest_incomplete_previous_buy_is_not_a_concrete_limit_or_session(tmp_path):
    case = backend_case(tmp_path)
    with closing(sqlite3.connect(case.market)) as con, con:
        con.execute("DELETE FROM jquants_master_snapshots")
    raw = scaffold_proposal(
        operation_id=case.operation.operation_id,
        proposal_id=PROPOSAL_ID,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    )
    raw["review"]["reviewer_identity"] = "synthetic-independent-reviewer"
    item = raw["items"][0]
    item.pop("blockers")
    item.pop("resume_conditions")
    item.update(
        action="buy",
        timing="wait_limit",
        terms=None,
        proposed_limit_yen="900",
        terms_unavailable_reasons=[
            {"code": "security_classification_unavailable", "message": "合成：分類未確認。"}
        ],
    )
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(bind_review(raw, {case.pair.assessment_id: case.pair.document}))
    row = _context(case).previous_buy_context["targets"]["1234"]
    assert row["status"] == "available"
    assert row["proposal_ref"]["proposal_id"] == saved.proposal_id
    assert row["previous_terms"] is None
    assert row["previous_proposed_limit_yen"] == "900"
    assert [reason["code"] for reason in row["terms_unavailable_reasons"]] == [
        "security_classification_unavailable"
    ]


def test_holding_context_does_not_look_up_asset_as_a_new_repeat_candidate(tmp_path):
    case = backend_case(tmp_path, holding=True)
    assert _context(case).previous_buy_context["targets"] == {}
