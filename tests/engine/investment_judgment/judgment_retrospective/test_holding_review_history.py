"""A new holding review starts from the earlier question, not from its trade instruction."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, timedelta

import pytest
from tests.engine.investment_judgment.judgment_retrospective.test_judgment_retrospective_persistence import (
    LESSON,
    NOW,
    _draft,
    _save,
)
from tests.helpers.proposal import PUBLISHED, backend_case, bind_review, reviewed_proposal

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    ProposalCorrectionDraft,
    ProposalReference,
)
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.portfolio.ledger.events import ExecutionEvent
from baibai_engine.portfolio.ledger.store import LedgerStoreService

STEP = timedelta(microseconds=1)


def _publish(case, number=1, *, action="hold", stamp=PUBLISHED):
    draft = reviewed_proposal(
        case,
        action=action,
        proposal_id=f"investment-proposal-00000000-0000-4000-8000-{number:012d}",
    )
    if action == "sell":
        raw = draft.payload()
        raw["items"][0].update(
            reason_code="consider_full_recovery",
            remaining_reward=None,
            terms=None,
            terms_unavailable_reasons=[
                {
                    "code": "conditional_recovery_not_order_terms",
                    "message": "配当余力の変化を比較。注文ではない。",
                }
            ],
        )
        draft = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    return TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: stamp).publish(
        draft
    )


def _context(case, capsys, when, *options, scope="review_positions"):
    before = (case.app.read_bytes(), case.runs.read_bytes())
    args = [
        "selection-context",
        "--request",
        "分配余力の変化で保有判断が変わるか",
        "--scope",
        scope,
        "--known-at",
        when.isoformat(),
        "--generation-id",
        case.selected.inputs.generation.generation_id,
        "--db",
        str(case.app),
        "--runs-path",
        str(case.runs),
        *options,
    ]
    assert main(args, now=when) == 0
    assert before == (case.app.read_bytes(), case.runs.read_bytes())
    return json.loads(capsys.readouterr().out)


def _record(case, when, side, qty=100):
    store = LedgerStoreService(case.app)
    ledger, head = store.load_with_head()
    event = ExecutionEvent(
        type="execution",
        event_id=f"recorded-{head}",
        execution_id=f"fill-{head}",
        occurred_at=when,
        side=side,
        ticker="1234",
        quantity=qty,
        price_yen="900",
        sector="情報・通信業",
        common_factors=("domestic-demand",),
    )
    store.apply_document(
        expected_head=head,
        expected_document=ledger,
        replacement=ledger.model_copy(update={"events": (*ledger.events, event), "as_of": when}),
    )


@pytest.mark.parametrize("action", ["hold", "defer", "sell"])
def test_review_starts_from_exact_holding_item_and_its_authored_follow_up(tmp_path, capsys, action):
    case = backend_case(tmp_path, holding=True)
    proposal = _publish(case, action=action)
    before = _context(case, capsys, PUBLISHED - STEP)
    assert before["holding_review_history"]["items"]["1234"] == {"status": "not_recorded"}
    doc = _context(case, capsys, PUBLISHED.astimezone(UTC), "--compare-ticker", "5555")
    review = doc["holding_review_history"]
    assert review["current_ledger_as_of"] == case.ledger.as_of.isoformat()
    assert set(review["items"]) == {"1234"}
    row = review["items"]["1234"]
    assert row["status"] == "same_recorded_holding_episode"
    assert row["original_action"] == action
    assert row["origin"]["proposal_id"] == proposal.proposal_id
    assert row["origin"]["draft_sha256"] == proposal.review.draft_sha256
    assert row["item_index"] == 0
    item = proposal.items[0]
    assert row["original_rationale"] == item.rationale
    assert row["original_review_conditions"] == list(item.review_conditions)
    if action == "sell":
        assert row["original_sell_reason"] == "consider_full_recovery"
    if action == "defer":
        assert row["original_resume_conditions"] == list(item.resume_conditions)
    assert {"terms", "quantity", "current_suggestion", "current_price"}.isdisjoint(row)
    assert row["follow_up_coverage"] == "not_recorded"
    assert "holding_review_history" not in _context(
        case, capsys, NOW, "--ticker", "1234", scope="specified_stocks"
    )
    # Preserve a later author's lesson even if it only corrected the reading of the old source.
    note = _draft(case.app, proposal_id=proposal.proposal_id, ticker="1234")
    note["document"].update(comparisons=[], lesson=LESSON)
    recorded = _save(case.app, note)
    assert not _context(case, capsys, NOW - STEP)["holding_review_history"]["items"]["1234"][
        "follow_up_notes"
    ]
    at = _context(case, capsys, NOW)["holding_review_history"]["items"]["1234"]
    assert at["follow_up_coverage"] == "lesson_only"
    assert at["follow_up_notes"] == [
        {
            "reference": recorded.reference.model_dump(mode="json"),
            "lesson": LESSON,
            "recorded_at": recorded.recorded_at.isoformat(),
        }
    ]


def test_filtered_scope_corrections_and_unreadable_never_promote_older_recommendation(
    tmp_path, capsys
):
    case = backend_case(tmp_path, holding=True)
    earlier = _publish(case)
    recent = _publish(case, 2, stamp=PUBLISHED + STEP)

    def read(when):
        return _context(case, capsys, when)["holding_review_history"]["items"]["1234"]

    assert read(PUBLISHED + STEP)["origin"]["proposal_id"] == recent.proposal_id

    def ref(p):
        return ProposalReference(proposal_id=p.proposal_id, draft_sha256=p.review.draft_sha256)

    receipt = PUBLISHED + timedelta(minutes=1)
    recorder = ProposalRecordService(case.app, clock=lambda: receipt)
    recorder.correct(
        ProposalCorrectionDraft(
            correction_id="correction",
            erroneous_ref=ref(recent),
            replacement_ref=ref(earlier),
            reason="前回の記載を誤った。",
        ),
        expected_latest_ref=ref(recent),
    )
    assert read(receipt - STEP)["origin"]["proposal_id"] == recent.proposal_id
    assert read(receipt)["origin"]["proposal_id"] == earlier.proposal_id
    with closing(sqlite3.connect(case.app)) as conn, conn:
        conn.execute(
            "DELETE FROM investment_proposal_item WHERE proposal_id=?", (recent.proposal_id,)
        )
    assert read(PUBLISHED)["origin"]["proposal_id"] == earlier.proposal_id
    assert read(PUBLISHED + STEP) == {"status": "unreadable_publication"}


def test_sold_then_repurchased_is_not_the_same_episode_even_at_identical_price_and_quantity(
    tmp_path, capsys
):
    case = backend_case(tmp_path, holding=True)
    proposal = _publish(case)
    _record(case, PUBLISHED + STEP, "sell")
    assert _context(case, capsys, PUBLISHED + STEP)["holding_review_history"]["items"] == {}
    _record(case, PUBLISHED + STEP * 2, "buy")
    current = _context(case, capsys, PUBLISHED + STEP * 2)["holding_review_history"]["items"][
        "1234"
    ]
    assert current["status"] == "different_recorded_holding_episode"
    assert current["origin"]["proposal_id"] == proposal.proposal_id
    assert "original_review_conditions" not in current
    # Replaying the ledger to the older fact cutoff does not claim that this is its recorded head then.
    old = _context(case, capsys, PUBLISHED)["holding_review_history"]
    assert old["current_ledger_as_of"] == (PUBLISHED + STEP * 2).isoformat()
    assert old["items"]["1234"]["status"] == "same_recorded_holding_episode"


def test_new_buy_for_the_same_issuer_is_not_a_prior_holding_review(tmp_path, capsys):
    case = backend_case(tmp_path)
    _publish(case, action="buy")
    _record(case, PUBLISHED + STEP, "buy")
    assert _context(case, capsys, PUBLISHED + STEP)["holding_review_history"]["items"] == {
        "1234": {"status": "not_recorded"}
    }
