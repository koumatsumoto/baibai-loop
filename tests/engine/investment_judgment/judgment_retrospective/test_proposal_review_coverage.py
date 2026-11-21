"""An exact published item's follow-up is neither a price observation nor a fill."""

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import UTC, timedelta

import pytest
from tests.engine.investment_judgment.judgment_retrospective.test_judgment_retrospective_persistence import (
    LESSON,
    NOW,
    UNRESOLVED,
    _draft,
    _publish,
    _save,
)
from tests.helpers.proposal import PUBLISHED, backend_case
from tests.helpers.proposal_records import not_purchased_report, ref
from tests.helpers.proposal_records import (
    records_case as records_case,
)

from baibai_engine.cli import investment_judgment
from baibai_engine.investment_judgment.judgment_retrospective.coverage import (
    proposal_review_coverage,
)
from baibai_engine.investment_judgment.judgment_retrospective.store import RetrospectiveReadError

STEP = timedelta(microseconds=1)


def _coverage(case, proposal, instant):
    return proposal_review_coverage(
        proposal_id=proposal.proposal_id, known_at=instant, db_path=case.app
    )


def test_all_items_and_recorded_conclusions_are_not_new_evidence_or_execution(tmp_path, capsys):
    case = backend_case(tmp_path, tickers=("1234", "5678"))
    case.operations.record_unfinished(
        case.operation.operation_id,
        ticker="5678",
        reason="親会社帰属を未確認",
        references=("synthetic:missing",),
        now=PUBLISHED - timedelta(minutes=12),
    )
    proposal = _publish(case, action="no_action")
    raw = _draft(case.app, proposal_id=proposal.proposal_id, ticker="1234")
    raw["document"]["comparisons"] = [
        {
            **UNRESOLVED,
            "result": "supported",
            "observed": "元の価格資料を読み直した",
            "reason": "当初の価格単位だけを確認した。後日の事業実績は未観測。",
            "citations": [
                {
                    "reference": proposal.items[0].assessment_ref.model_dump(mode="json"),
                    "source_ids": ["price"],
                    "fact_ids": ["close"],
                }
            ],
        }
    ]
    raw["document"]["lesson"] = LESSON
    first = _save(case.app, raw)
    correction = deepcopy(raw)
    correction["retrospective_id"] = "revision"
    correction["document"]["corrects_ref"] = first.reference.model_dump(mode="json")
    correction["document"]["comparisons"] = [UNRESOLVED]
    corrected = _save(case.app, correction, now=NOW + STEP)
    # A later note about the same assessment is not about this published proposal item.
    _save(
        case.app,
        _draft(case.app, identity="assessment-only", assessment_id=case.pair.assessment_id),
    )
    before = case.app.read_bytes()
    then = _coverage(case, proposal, NOW - STEP)
    assert [r["original_action"] for r in then["items"]] == ["no_action", "defer"]
    assert all("original_session_limit" not in row for row in then["items"])
    assert then["item_counts_by_coverage"] == {"not_recorded": 2}
    assert then["items"][1]["original_review_conditions"] == list(
        proposal.items[1].review_conditions
    )

    deferred = proposal.items[1]
    assert deferred.review_conditions != deferred.resume_conditions
    assert then["items"][1]["original_resume_conditions"] == list(deferred.resume_conditions)
    assert then["items"][1]["original_blockers"] == [
        b.model_dump(mode="json") for b in deferred.blockers
    ]
    assert "original_resume_conditions" not in then["items"][0]

    def cli_at(stamp):
        assert (
            investment_judgment.main(
                [
                    "judgment-retrospective",
                    "coverage",
                    "--proposal-id",
                    proposal.proposal_id,
                    "--known-at",
                    stamp.astimezone(UTC).isoformat(),
                    "--db",
                    str(case.app),
                ],
                now=NOW + STEP,
            )
            == 0
        )
        return json.loads(capsys.readouterr().out)

    at = cli_at(NOW)
    assert at["item_count"] == 2
    assert at["item_counts_by_coverage"] == {"authored_conclusion_recorded": 1, "not_recorded": 1}
    assert at["draft_sha256"] == proposal.review.draft_sha256
    note = at["items"][0]["retrospectives"]
    assert len(note) == 1
    assert note[0]["lesson"] == LESSON
    assert note[0]["authored_comparisons"] == [
        c.model_dump(mode="json") for c in first.document.comparisons
    ]
    assert not note[0]["superseded_at_cutoff"]
    later = cli_at(NOW + STEP)
    assert later["item_counts_by_coverage"] == {"unresolved_comparisons_only": 1, "not_recorded": 1}
    prior, update = later["items"][0]["retrospectives"]
    assert prior["superseded_at_cutoff"]
    assert not update["superseded_at_cutoff"]
    assert update["corrects_ref"] == prior["reference"]
    assert update["reference"] == corrected.reference.model_dump(mode="json")
    assert case.app.read_bytes() == before


def test_erroneous_publication_and_human_receipt_are_not_item_outcomes(records_case):
    case, earlier, wrong, recorder, correction = records_case
    cutoff = recorder.correct(correction, expected_latest_ref=ref(wrong)).recorded_at
    recorder.report(not_purchased_report(wrong))
    raw = _draft(case.app, proposal_id=earlier.proposal_id, ticker="1234")
    raw["document"]["comparisons"] = []
    raw["document"]["lesson"] = LESSON
    _save(case.app, raw)
    assert _coverage(case, earlier, NOW)["item_counts_by_coverage"] == {"lesson_only": 1}
    assert _coverage(case, wrong, cutoff - STEP)["proposal_status"] == "published"
    for known_at in (cutoff, NOW):
        view = _coverage(case, wrong, known_at)
        assert view["proposal_status"] == "corrected_as_erroneous"
        assert view["item_counts_by_coverage"] == {"not_recorded": 1}
        assert view["items"][0]["original_action"] == "no_action"
    with pytest.raises(RetrospectiveReadError, match="not available"):
        _coverage(case, wrong, wrong.published_at - STEP)


def test_unreadable_is_never_counted_as_none_and_later_body_not_opened(tmp_path):
    case = backend_case(tmp_path, tickers=("1234", "5678"))
    case.operations.record_unfinished(
        case.operation.operation_id,
        ticker="5678",
        reason="未完",
        references=("test:pending",),
        now=PUBLISHED - timedelta(minutes=12),
    )
    proposal = _publish(case, action="no_action")
    note = _save(case.app, _draft(case.app, proposal_id=proposal.proposal_id, ticker="5678"))
    with closing(sqlite3.connect(case.app)) as c:
        c.execute("DROP TRIGGER judgment_retrospective_immutable")
        c.execute(
            "UPDATE judgment_retrospective SET payload_sha256=? WHERE retrospective_id=?",
            ("0" * 64, note.reference.retrospective_id),
        )
        c.commit()
    assert _coverage(case, proposal, NOW - STEP)["item_counts_by_coverage"] == {"not_recorded": 2}
    at = _coverage(case, proposal, NOW)
    assert [(r["ticker"], r["coverage"]) for r in at["items"]] == [
        ("1234", "not_recorded"),
        ("5678", "unreadable"),
    ]
