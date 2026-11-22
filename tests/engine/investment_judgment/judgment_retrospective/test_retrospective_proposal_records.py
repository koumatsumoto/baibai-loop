"""本人の非購入理由と提案訂正を、当時判断/台帳事実から分けて振り返る。"""

import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import UTC, timedelta

import pytest
from tests.helpers.proposal import PUBLISHED
from tests.helpers.proposal_records import (
    not_purchased_report,
    ref,
    snapshot,
)
from tests.helpers.proposal_records import (
    records_case as records_case,
)

from baibai_engine.investment_judgment.judgment_retrospective.document import RetrospectiveDraft
from baibai_engine.investment_judgment.judgment_retrospective.query import (
    get_retrospective,
    scaffold_retrospective,
)
from baibai_engine.investment_judgment.judgment_retrospective.store import RetrospectiveStoreService
from baibai_engine.investment_judgment.trade_proposal.query import ProposalRecordReader
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService

NOW = PUBLISHED + timedelta(hours=2)


def _scaffold(case, proposal=None, *, now=NOW, identity="synthetic-records-retro", as_of=None):
    return scaffold_retrospective(
        retrospective_id=identity,
        proposal_id=None if proposal is None else proposal.proposal_id,
        assessment_id=case.pair.assessment_id if proposal is None else None,
        ticker=None if proposal is None else "1234",
        as_of=as_of or now.date(),
        milestone="本人の資金事情と元の価値/価格条件を分けて確認",
        db_path=case.app,
        known_at=now,
    )


def _save(case, scaffold, *, now=NOW):
    raw = deepcopy(scaffold["draft"])
    raw["document"]["lesson"] = {
        "next_time": "非購入理由を読み、注文/約定と企業仮説の結果を混同しない",
        "rationale": "非購入は確認できても、未発注/取消/未約定を特定する台帳根拠はない",
    }
    draft = RetrospectiveDraft.model_validate(raw)
    saved = RetrospectiveStoreService(case.app, clock=lambda: now).save(draft)
    return draft, saved


def test_normal_reports_corrections_scaffold_and_exact_revisit_keep_separate_meanings(records_case):
    case, original, wrong, service, correction = records_case
    before = snapshot(case.app)
    corrected = service.correct(correction, expected_latest_ref=ref(wrong))
    report = service.report(not_purchased_report(original))
    scaffold = _scaffold(case, original)
    origin = scaffold["context"]["origin"]
    supplement = origin["proposal_records"]
    assert supplement == {
        "source": "proposal_receipts",
        "scope": "whole_proposal",
        "recorded_through": NOW.astimezone(UTC).isoformat(),
        "status": "retained",
        "records": {
            "corrections": [corrected.model_dump(mode="json")],
            "human_reports": [report.model_dump(mode="json")],
        },
    }
    assert origin["proposal"]["item"] == original.items[0].model_dump(mode="json")
    assert origin["proposal"]["item"]["action"] == "buy"
    assert report.subject_at is None
    assert scaffold["context"]["ledger"]["events"] == []
    assert snapshot(case.app) == before
    draft, saved = _save(case, scaffold)
    result = get_retrospective(retrospective_id=saved.reference.retrospective_id, db_path=case.app)
    assert result["record"] == saved.model_dump(mode="json")
    assert result["context"]["origin"]["proposal_records"]["records"] == supplement["records"]
    assert (
        RetrospectiveStoreService(case.app, clock=lambda: NOW + timedelta(days=1)).save(draft)
        == saved
    )
    assert snapshot(case.app) == before
    supplement["records"]["human_reports"][0]["reason_quote"] = "返却値だけを編集"
    assert (
        _scaffold(case, original)["context"]["origin"]["proposal_records"]["records"][
            "human_reports"
        ][0]["reason_quote"]
        == report.reason_quote
    )


def test_later_receipts_never_backfill_old_revisit_and_do_not_replace_erroneous_origin(
    records_case,
):
    case, _original, wrong, _service, correction = records_case
    before = _scaffold(case, wrong)
    draft, saved = _save(case, before)
    late = NOW + timedelta(days=1)
    writer = ProposalRecordService(case.app, clock=lambda: late)
    corrected = writer.correct(correction, expected_latest_ref=ref(wrong))
    report = writer.report(not_purchased_report(wrong, reported_at=late - timedelta(hours=1)))
    stored = get_retrospective(retrospective_id=saved.reference.retrospective_id, db_path=case.app)
    assert stored["record"] == saved.model_dump(mode="json")
    assert stored["context"]["origin"]["proposal_records"]["records"] == {
        "corrections": [],
        "human_reports": [],
    }
    assert RetrospectiveStoreService(case.app, clock=lambda: late).save(draft) == saved
    new = _scaffold(case, wrong, now=late)
    assert new["context"]["origin"]["proposal_records"]["records"] == {
        "corrections": [corrected.model_dump(mode="json")],
        "human_reports": [report.model_dump(mode="json")],
    }
    assert new["context"]["origin"]["proposal"]["proposal_id"] == wrong.proposal_id
    assert new["context"]["origin"]["proposal"]["item"]["action"] == "no_action"
    assert new["context"]["ledger"] == before["context"]["ledger"]


@pytest.mark.parametrize("damage", ["body", "clock"])
def test_exact_revisit_checks_only_receipts_within_the_saved_cutoff(records_case, damage):
    case, original, _, service, _ = records_case
    report = service.report(not_purchased_report(original))
    _, saved = _save(case, _scaffold(case, original))
    later = NOW + timedelta(microseconds=1)
    late = ProposalRecordService(case.app, clock=lambda: later.astimezone(UTC)).report(
        not_purchased_report(original, identity="late-receipt")
    )
    with closing(sqlite3.connect(case.app)) as connection, connection:
        connection.execute("DROP TRIGGER proposal_human_report_no_update")
        if damage == "body":
            connection.execute(
                "UPDATE proposal_human_report SET payload='{}' WHERE report_id=?",
                (late.report_id,),
            )
        else:
            connection.execute(
                "UPDATE proposal_human_report SET recorded_at=? WHERE report_id=?",
                (later.replace(tzinfo=None).isoformat(), late.report_id),
            )
    before = case.app.read_bytes()
    revisited = get_retrospective(
        retrospective_id=saved.reference.retrospective_id, db_path=case.app
    )
    assert revisited["record"] == saved.model_dump(mode="json")
    old = revisited["context"]["origin"]["proposal_records"]
    if damage == "body":
        assert old["status"] == "retained"
        assert old["records"]["human_reports"] == [report.model_dump(mode="json")]
    else:
        assert old["status"] == "unavailable"  # An unreadable clock cannot be excluded as late.
        assert old["records"] is None
    at_receipt = _scaffold(case, original, now=later)["context"]["origin"]["proposal_records"]
    assert at_receipt["status"] == "unavailable"
    assert at_receipt["records"] is None
    assert case.app.read_bytes() == before


def test_exact_origin_receipt_cutoff_preserves_offset_microseconds_and_unknown_subject(
    records_case,
):
    case, original, wrong, _service, _correction = records_case
    late = NOW + timedelta(days=2)
    # Receipt time and the report's target/information time are not interchangeable.
    writer = ProposalRecordService(case.app, clock=lambda: late)
    old_report = writer.report(
        not_purchased_report(original, reported_at=late - timedelta(hours=1))
    )
    writer.report(not_purchased_report(wrong, identity="other-proposal"))
    future = ProposalRecordService(case.app, clock=lambda: late + timedelta(microseconds=1))
    future.report(not_purchased_report(original, identity="one-microsecond-later"))
    equivalent_cutoff = late.astimezone(UTC)
    result = _scaffold(case, original, now=equivalent_cutoff, as_of=PUBLISHED.date())
    records = result["context"]["origin"]["proposal_records"]["records"]
    assert records["human_reports"] == [old_report.model_dump(mode="json")]
    assert records["human_reports"][0]["subject_at"] is None
    assert old_report.reported_at.date() > PUBLISHED.date()
    assert records["human_reports"][0]["reported_at"] == old_report.reported_at.isoformat()
    assert result["context"]["ledger"]["events"] == []


def test_assessment_only_and_unreadable_optional_records_do_not_invent_report_or_break_old_judgment(
    records_case, monkeypatch
):
    case, original, _wrong, _service, _correction = records_case
    assert _scaffold(case)["context"]["origin"]["proposal_records"] is None

    def unavailable(*_args, **_kwargs):
        raise ValueError("synthetic broken supplementary receipt")

    monkeypatch.setattr(ProposalRecordReader, "read", unavailable)
    before = snapshot(case.app)
    result = _scaffold(case, original)
    assert result["context"]["origin"]["proposal_records"]["status"] == "unavailable"
    assert result["context"]["origin"]["proposal_records"]["records"] is None
    assert result["context"]["origin"]["proposal"]["item"]["action"] == "buy"
    assert snapshot(case.app) == before
    _, saved = _save(case, result)
    assert get_retrospective(retrospective_id=saved.reference.retrospective_id, db_path=case.app)[
        "record"
    ] == saved.model_dump(mode="json")
