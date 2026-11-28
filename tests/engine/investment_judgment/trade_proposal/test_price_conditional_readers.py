"""Exact A/B reader boundary: an author's ceiling is not a saved order limit."""

import json
from datetime import timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload
from tests.helpers.operation_proposal import seed_proposal
from tests.helpers.proposal import (
    PUBLISHED,
    backend_case,
    bind_review,
    reviewed_proposal,
)

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.judgment_retrospective.coverage import (
    original_item_questions,
)
from baibai_engine.investment_judgment.judgment_retrospective.query import scaffold_retrospective
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.contracts import BuyItem, TradeProposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.workflow.selection_evidence import read_selection_evidence

_MARKER = "price_conditional_allocation"
_NOW = PUBLISHED + timedelta(minutes=5)
_NEXT_ID = "investment-proposal-00000000-0000-4000-8000-000000000006"


@pytest.fixture
def signed_history(tmp_path):
    case = backend_case(tmp_path)
    a = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: PUBLISHED,
    ).publish(reviewed_proposal(case))

    def termless(number, *, code, economic_decision=None):
        raw = a.payload()
        raw["proposal_id"] = f"investment-proposal-00000000-0000-4000-8000-{number:012d}"
        if code == "security_classification_unavailable":
            raw["inputs"]["security_classification"] = []
        raw["items"][0].update(
            terms=None,
            proposed_limit_yen="1000",
            terms_unavailable_reasons=[{"code": code, "message": "保存時に原資料を確認できない。"}],
        )
        if economic_decision is not None:
            raw["items"][0]["economic_decision"] = economic_decision
        signed = bind_review(raw, {case.pair.assessment_id: case.pair.document})
        record = TradeProposal.model_validate(
            {**signed.payload(), "published_at": PUBLISHED + timedelta(minutes=number - 3)}
        )
        # Historical shape / simulated future output, not an enabled writer or real record.
        seed_proposal(case.app, record)
        return record

    old = termless(4, code="security_classification_unavailable")
    b = termless(5, code="order_session_unknown", economic_decision=_MARKER)
    return case, a, old, b


def _cli(capsys, *argv, now=_NOW):
    assert main(list(argv), now=now) == 0
    return json.loads(capsys.readouterr().out)


def test_current_preview_does_not_report_an_author_ceiling_as_a_saved_order(signed_history, capsys):
    case, a, old, b = signed_history
    before = case.app.read_bytes(), case.market.read_bytes()
    for record in (b, old, a):
        output = _cli(
            capsys,
            "trade-proposal-preview",
            "--proposal-id",
            record.proposal_id,
            "--db",
            str(case.app),
            "--sqlite-path",
            str(case.market),
            *case.listing_args,
        )
        row = output["items"][0]
        assert output["saved_proposal_id"] == record.proposal_id
        if record == a:
            assert row["saved_terms"]["quantity"] == 300
            assert row["saved_limit_revalidation"]["quantity"] == 300
            assert row["saved_ceiling_calculation"] is None
            continue
        assert output["status"] == "blocked"
        assert row["saved_terms"] is None
        assert row["saved_order_readiness"] == "needs_terms"
        assert row["saved_limit_revalidation"] is None
        assert row["saved_economic_decision"] == (_MARKER if record == b else None)
        (reason,) = row["saved_terms_unavailable_reasons"]
        assert reason == record.items[0].terms_unavailable_reasons[0].model_dump(mode="json")
        assert row["saved_proposed_limit_yen"] == "1000"
        calc = row["saved_ceiling_calculation"]
        assert calc["limit_price_projection"] is not None
        assert calc["quantity"] == row["current_quote_calculation"]["quantity"] == 0
        assert calc["maximum_quantity"] is calc["capital_comparison"] is None
        assert [why["code"] for why in row["blockers"]] == ["order_terms_unconfirmed"]
    assert (case.app.read_bytes(), case.market.read_bytes()) == before


def test_previous_buy_projection_keeps_b_not_a_after_calendar_is_known(signed_history, capsys):
    case, a, old, b = signed_history
    before = case.app.read_bytes()
    for saved in (a, old, b):
        output = _cli(
            capsys,
            "trade-proposal-context",
            "--operation-id",
            case.operation.operation_id,
            "--proposal-id",
            _NEXT_ID,
            "--db",
            str(case.app),
            "--sqlite-path",
            str(case.market),
            now=saved.published_at,
        )
        prior = output["previous_buy_context"]["targets"]["1234"]
        assert prior["proposal_ref"] == {
            "proposal_id": saved.proposal_id,
            "draft_sha256": saved.review.draft_sha256,
        }
        assert prior["previous_order_readiness"] == ("ready" if saved == a else "needs_terms")
        assert prior["previous_economic_decision"] == (_MARKER if saved == b else None)
        assert (prior["previous_terms"] is None) == (saved != a)
        assert (prior["previous_timing"] is None) == (saved != a)
    assert case.app.read_bytes() == before


def test_thin_follow_up_and_exact_manual_replay_do_not_reclassify_b(signed_history, capsys):
    case, a, old, b = signed_history
    document, review = pair_payload(review_number=90)
    AssessmentStoreService(case.app, clock=lambda: _NOW).publish_reviewed_assessment(
        assessment_id(90), document, review, supersedes_id=case.pair.assessment_id
    )
    before = case.app.read_bytes()
    changes = _cli(
        capsys,
        "judgment-retrospective",
        "changes",
        "--after",
        b.published_at.isoformat(),
        "--db",
        str(case.app),
    )
    (event,) = changes["events"]
    assert event["kind"] == "reviewed_assessment"
    assert [row["proposal_id"] for row in event["earlier_proposal_items"]] == [
        a.proposal_id,
        old.proposal_id,
        b.proposal_id,
    ]
    for original, saved in zip(event["earlier_proposal_items"], (a, old, b), strict=True):
        result = _cli(
            capsys,
            "judgment-retrospective",
            "coverage",
            "--proposal-id",
            saved.proposal_id,
            "--db",
            str(case.app),
        )
        for row in (result["items"][0], original):
            assert row["original_action"] == "buy"
            assert row["original_order_readiness"] == ("ready" if saved == a else "needs_terms")
            assert row["original_economic_decision"] == (_MARKER if saved == b else None)
            assert row["original_session_limit"] == (
                {"limit_price_yen": "1000", "target_session": "2026-10-15"} if saved == a else None
            )
    context = scaffold_retrospective(
        retrospective_id="synthetic-retrospective",
        proposal_id=b.proposal_id,
        ticker="1234",
        as_of=b.as_of,
        db_path=case.app,
        known_at=_NOW,
    )["context"]
    assert context["origin"]["proposal"]["item"] == b.model_dump(mode="json")["items"][0]
    assert context["ledger"]["events"] == []
    selection = read_selection_evidence(
        operation_id=case.operation.operation_id,
        proposal_id=b.proposal_id,
        sha256=b.research_scope.selection_sha256,
        db_path=case.app,
    )
    assert selection["selection_sha256"] == b.research_scope.selection_sha256
    assert case.app.read_bytes() == before


def test_follow_up_distinguishes_a_changed_limit_from_a_different_session(signed_history):
    _, saved, _, _ = signed_history
    for price, session in ((990, "2026-10-15"), (1000, "2026-10-15"), (1000, "2026-10-16")):
        raw = saved.items[0].model_dump(mode="json")
        raw["terms"].update(
            limit_price_yen=str(price),
            notional_yen=str(price * raw["terms"]["quantity"]),
            target_session=session,
        )
        row = original_item_questions(BuyItem.model_validate(raw))
        assert row["original_session_limit"] == {
            "limit_price_yen": str(price),
            "target_session": session,
        }
