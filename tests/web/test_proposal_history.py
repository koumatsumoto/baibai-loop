"""保存提案の履歴は銘柄で上書きせず、壊れた一件を空履歴で隠さない。"""

import sqlite3
from contextlib import closing
from datetime import UTC, timedelta

import pytest
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.investment_judgment.trade_proposal.contracts import proposal_draft_sha256
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    ProposalCorrectionDraft,
    ProposalReference,
)
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.storage.sqlite.application.write import initialize_database
from baibai_web.readmodel.builders.proposal import build_proposal_history
from baibai_web.sources.proposal import DbProposalSource

SECOND = "investment-proposal-00000000-0000-4000-8000-000000000004"


def _two_publications(tmp_path):
    case = backend_case(tmp_path)
    first = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case))
    second = TradeProposalService(
        case.app,
        sqlite_path=case.market,
        clock=lambda: (PUBLISHED + timedelta(hours=1)).astimezone(UTC),
    ).publish(reviewed_proposal(case, action="no_action", proposal_id=SECOND))
    return case, first, second


def test_history_preserves_each_publication_exact_pair_and_saved_identity(tmp_path):
    case, first, second = _two_publications(tmp_path)
    before = case.app.read_bytes()
    # No current market data, recreated judgment or ticker-level overwrite is necessary.
    case.market.unlink()
    view = build_proposal_history(DbProposalSource(case.app), generated_at=PUBLISHED)
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
    source = DbProposalSource(case.app)
    assert source.latest().proposal_id == first.proposal_id
    view = build_proposal_history(source)
    assert [row.proposal_id for row in view.proposals] == [second.proposal_id, first.proposal_id]
    assert all(row.corrections == [record] for row in view.proposals)


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
    view = build_proposal_history(DbProposalSource(case.app))
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


@pytest.mark.parametrize("state", ["empty", "missing", "broken"])
def test_empty_history_is_not_read_failure(tmp_path, state):
    app = tmp_path / "app.sqlite"
    if state == "empty":
        initialize_database(app)
    elif state == "broken":
        app.write_bytes(b"not a sqlite database")
    view = build_proposal_history(DbProposalSource(app))
    assert view.status == ("empty" if state == "empty" else "unavailable")
    assert not view.proposals
    assert not view.unavailable_proposal_ids
    assert (view.error is None) == (state == "empty")
