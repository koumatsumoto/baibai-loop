"""保存提案の履歴は銘柄で上書きせず、壊れた一件を空履歴で隠さない。"""

import sqlite3
from contextlib import closing
from datetime import UTC, timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.contracts import proposal_draft_sha256
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    ProposalCorrectionDraft,
    ProposalReference,
)
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.primitives.time import JST
from baibai_web.sources import proposal as proposal_source
from baibai_web.sources.proposal import read_proposal_views

SECOND = "investment-proposal-00000000-0000-4000-8000-000000000004"


def _publish_first(case):
    return TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        reviewed_proposal(case)
    )


def _publish_second(case):
    return TradeProposalService(
        case.app,
        sqlite_path=case.market,
        clock=lambda: (PUBLISHED + timedelta(hours=1)).astimezone(UTC),
    ).publish(reviewed_proposal(case, action="no_action", proposal_id=SECOND))


def _two_publications(tmp_path):
    case = backend_case(tmp_path)
    return case, _publish_first(case), _publish_second(case)


def test_history_preserves_each_publication_exact_pair_and_saved_identity(tmp_path, mocker):
    case, first, second = _two_publications(tmp_path)
    document, review = pair_payload(review_number=90)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(hours=2)
    ).publish_reviewed_assessment(
        assessment_id(90), document, review, supersedes_id=case.pair.assessment_id
    )
    before = case.app.read_bytes()
    # Re-read the published pair, not the later review, even without live stores.
    case.market.unlink()
    case.runs.unlink()
    connect = mocker.spy(proposal_source, "connect_read_only")
    records = mocker.spy(proposal_source, "ProposalRecordReader")
    project = mocker.spy(proposal_source, "read_saved_proposal_view")
    generated_at = PUBLISHED + timedelta(hours=3)
    latest, view = read_proposal_views(case.app, generated_at=generated_at)
    assert connect.call_count == records.call_count == 1
    assert [call.args[1] for call in project.call_args_list] == sorted(
        [first.proposal_id, second.proposal_id]
    )
    assert project.call_args_list[0].args[0] is project.call_args_list[1].args[0]
    assert latest.status == "ready"
    assert latest.proposal == view.proposals[0]
    assert latest.generated_at == view.generated_at == generated_at
    assert latest.proposal is not None
    assert latest.proposal.items[0].assessment.valuation.price_yen == str(
        case.pair.document.input_snapshot.facts[0].value
    )
    assert view.status == "ready"
    assert [row.proposal_id for row in view.proposals] == [second.proposal_id, first.proposal_id]
    assert [row.items[0].ticker for row in view.proposals] == ["1234", "1234"]
    assert [row.items[0].action for row in view.proposals] == ["no_action", "buy"]
    for proposal in view.proposals:
        item = proposal.items[0]
        assert item.company_name == case.pair.document.input_snapshot.company_name
        assert item.sector == case.pair.document.input_snapshot.sector
        assert item.assessment.assessment_id == case.pair.assessment_id
    assert view.proposals[1].items[0].terms.limit_price_yen == "1000"
    assert case.app.read_bytes() == before
    assert not case.market.exists()
    assert not case.runs.exists()


def test_generation_clock_follows_the_read_snapshot_when_a_proposal_is_published(tmp_path, mocker):
    case = backend_case(tmp_path)
    first = _publish_first(case)
    clock = mocker.patch.object(proposal_source, "datetime").now
    clock.return_value = PUBLISHED + timedelta(minutes=30)
    read_ids = proposal_source.published_proposal_ids
    generated_at = PUBLISHED + timedelta(hours=1, seconds=1)

    def read_ids_after_publication(connection):
        assert connection.in_transaction
        _publish_second(case)
        clock.return_value = generated_at
        return read_ids(connection)

    mocker.patch.object(
        proposal_source, "published_proposal_ids", side_effect=read_ids_after_publication
    )
    latest, history = read_proposal_views(case.app)
    assert latest.proposal == history.proposals[0]
    assert [row.proposal_id for row in history.proposals] == [SECOND, first.proposal_id]
    assert history.proposals[0].published_at < generated_at
    assert latest.generated_at == history.generated_at == generated_at
    clock.assert_called_once_with(JST)


def test_history_keeps_corrected_original_visible_with_its_correction(tmp_path):
    case, first, second = _two_publications(tmp_path)

    def ref(row):
        return ProposalReference(
            proposal_id=row.proposal_id, draft_sha256=proposal_draft_sha256(row)
        )

    record = ProposalRecordService(case.app, clock=lambda: PUBLISHED + timedelta(days=2)).correct(
        ProposalCorrectionDraft(
            correction_id="synthetic-history-correction",
            erroneous_ref=ref(second),
            replacement_ref=ref(first),
            reason="合成の訂正理由。",
        ),
        expected_latest_ref=ref(second),
    )
    latest, view = read_proposal_views(case.app)
    assert latest.status == "ready"
    assert latest.proposal is not None
    assert latest.proposal.proposal_id == first.proposal_id
    assert [row.proposal_id for row in view.proposals] == [second.proposal_id, first.proposal_id]
    assert all(row.corrections == [record] for row in view.proposals)


def test_broken_shared_correction_does_not_lose_the_unreadable_ids(tmp_path):
    case, first, second = _two_publications(tmp_path)
    with closing(sqlite3.connect(case.app)) as connection, connection:
        connection.execute(
            "INSERT INTO proposal_correction VALUES (?,?,?,?,?)",
            ("broken", second.proposal_id, first.proposal_id, PUBLISHED.isoformat(), "{}"),
        )
    latest, history = read_proposal_views(case.app)
    assert latest.status == history.status == "unavailable"
    assert latest.proposal is None
    assert history.proposals == []
    assert history.unavailable_proposal_ids == sorted([first.proposal_id, second.proposal_id])


@pytest.mark.parametrize("damage_all", [False, True])
def test_damaged_publication_does_not_hide_other_reports_or_claim_full_history(
    tmp_path, damage_all
):
    case, first, second = _two_publications(tmp_path)
    with closing(sqlite3.connect(case.app)) as connection, connection:
        connection.execute("DROP TRIGGER investment_proposal_immutable")
        for proposal_id in (
            [first.proposal_id, second.proposal_id] if damage_all else [second.proposal_id]
        ):
            connection.execute(
                "UPDATE investment_proposal SET draft_sha256=? WHERE proposal_id=?",
                ("0" * 64, proposal_id),
            )
    before = case.app.read_bytes()
    latest, view = read_proposal_views(case.app)
    assert latest.status == "unavailable"
    assert latest.proposal is None
    assert latest.error == "保存済み提案を読み取れません。"
    assert str(case.app) not in latest.model_dump_json()
    assert view.status == ("unavailable" if damage_all else "partial")
    assert [row.proposal_id for row in view.proposals] == (
        [] if damage_all else [first.proposal_id]
    )
    assert set(view.unavailable_proposal_ids) == (
        {first.proposal_id, second.proposal_id} if damage_all else {second.proposal_id}
    )
    assert view.error == "一部の保存済み提案を読み取れません。"
    assert str(case.app) not in view.model_dump_json()
    assert case.app.read_bytes() == before


def test_history_index_failure_still_projects_the_selected_publication_once(tmp_path, mocker):
    case, _, second = _two_publications(tmp_path)
    mocker.patch.object(
        proposal_source, "published_proposal_ids", side_effect=sqlite3.OperationalError("private")
    )
    project = mocker.spy(proposal_source, "read_saved_proposal_view")
    latest, history = read_proposal_views(case.app, generated_at=second.published_at)
    assert latest.status == "ready"
    assert latest.proposal is not None
    assert latest.proposal.proposal_id == second.proposal_id
    assert project.call_count == 1
    assert history.status == "unavailable"
    assert history.proposals == history.unavailable_proposal_ids == []
    assert "private" not in history.model_dump_json()
