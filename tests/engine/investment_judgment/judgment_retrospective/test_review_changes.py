"""Window new saved evidence, not the original issuer event or a trade outcome."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, timedelta

import pytest
from tests.engine.investment_judgment.company_research.test_persistence import draft
from tests.engine.investment_judgment.judgment_retrospective.test_judgment_retrospective_persistence import (
    _assessment,
    _publish,
)
from tests.engine.investment_judgment.trade_proposal.test_proposal_records import (
    records_case as records_case,
)
from tests.engine.investment_judgment.trade_proposal.test_proposal_records import ref
from tests.helpers.proposal import PUBLISHED, backend_case

from baibai_engine.cli import investment_judgment
from baibai_engine.investment_judgment.company_research.store import ResearchStoreService
from baibai_engine.investment_judgment.judgment_retrospective.changes import review_changes

TICK = timedelta(microseconds=1)


def test_adjacent_receipt_windows_do_not_promote_old_facts_or_same_time_judgments(tmp_path, capsys):
    case = backend_case(tmp_path, tickers=("1234", "5678"))
    case.operations.record_unfinished(
        case.operation.operation_id,
        ticker="5678",
        reason="原資料待ち",
        references=("source:missing",),
        now=PUBLISHED - timedelta(minutes=12),
    )
    proposal = _publish(case, action="no_action")
    t = PUBLISHED + TICK
    prior = ResearchStoreService(case.app, clock=lambda: PUBLISHED).save(
        draft("before-or-at", ticker="5678", evidence=True)
    )
    later = ResearchStoreService(case.app, clock=lambda: t.astimezone(UTC)).save(
        draft("same-facts", ticker="5678", evidence=True, prior=prior.reference)
    )
    ResearchStoreService(case.app, clock=lambda: t).save(draft("unrelated", ticker="9999"))
    assessed = _assessment(case.app, number=3, ticker="5678", published_at=t)
    original = case.app.read_bytes()

    def cli(after, through):
        assert (
            investment_judgment.main(
                [
                    "judgment-retrospective",
                    "changes",
                    "--after",
                    after.isoformat(),
                    "--known-at",
                    through.isoformat(),
                    "--db",
                    str(case.app),
                ],
                now=t + TICK,
            )
            == 0
        )
        return json.loads(capsys.readouterr().out)

    assert cli(PUBLISHED - TICK, PUBLISHED)["events"] == []
    view = cli(PUBLISHED, t)
    assert later.document == prior.document
    assert [x["kind"] for x in view["events"]] == ["company_research", "reviewed_assessment"]
    r, a = view["events"]
    assert r["reference"] == later.reference.model_dump(mode="json")
    assert a["reference"]["core_sha256"] == assessed.core_sha256
    assert r["information_as_of"] == "2026-10-14"
    assert r["earlier_proposal_items"] == a["earlier_proposal_items"]
    [origin] = r["earlier_proposal_items"]
    assert (origin["proposal_id"], origin["draft_sha256"], origin["item_index"]) == (
        proposal.proposal_id,
        proposal.review.draft_sha256,
        1,
    )
    assert origin["original_action"] == "defer"
    assert origin["original_resume_conditions"] == list(proposal.items[1].resume_conditions)
    assert "result" not in r
    assert view["unreadable"] == []
    assert view["next_after"] == t.isoformat()
    assert cli(t.astimezone(UTC), t + TICK)["events"] == []
    assert case.app.read_bytes() == original


def test_distinct_origins_and_correction_keep_their_own_receipts(records_case):
    case, valid, erroneous, recorder, correction = records_case
    when = recorder.correct(correction, expected_latest_ref=ref(erroneous)).recorded_at
    ResearchStoreService(case.app, clock=lambda: when - TICK).save(draft("later"))

    def statuses(at):
        view = review_changes(after=PUBLISHED, known_at=at, db_path=case.app)
        [event] = view["events"]
        return {i["proposal_id"]: i["proposal_status"] for i in event["earlier_proposal_items"]}

    assert statuses(when - TICK) == {
        valid.proposal_id: "published",
        erroneous.proposal_id: "published",
    }
    assert statuses(when) == {
        valid.proposal_id: "published",
        erroneous.proposal_id: "corrected_as_erroneous",
    }


def test_unreadable_in_window_does_not_advance_checkpoint_or_hide_intact_document(tmp_path):
    case = backend_case(tmp_path)
    _publish(case)
    r = ResearchStoreService(case.app, clock=lambda: PUBLISHED + TICK).save(draft("good"))
    ResearchStoreService(case.app, clock=lambda: PUBLISHED + 2 * TICK).save(
        draft("broken", prior=r.reference)
    )
    with closing(sqlite3.connect(case.app)) as conn:
        conn.execute("DROP TRIGGER company_research_immutable")
        conn.execute(
            "UPDATE company_research SET payload_sha256=? WHERE research_id='broken'", ("0" * 64,)
        )
        conn.commit()

    def check(t):
        return review_changes(after=PUBLISHED, known_at=t, db_path=case.app)

    before = check(PUBLISHED + TICK)
    assert before["unreadable"] == []
    assert before["next_after"]
    after = check(PUBLISHED + 2 * TICK)
    assert [e["reference"] for e in after["events"]] == [r.reference.model_dump(mode="json")]
    assert after["unreadable"] == [
        {"kind": "company_research", "id": "broken", "reason": "document_unreadable"}
    ]
    assert after["next_after"] is None
    with pytest.raises(ValueError, match="precede"):
        check(PUBLISHED)
