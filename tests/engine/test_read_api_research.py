"""正確な保存pairと当初評価を読み、破損版を旧版で隠さない。"""

import json
import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, review_id, update_review_hash
from tests.helpers.proposal import PREPARED, PUBLISHED, backend_case, reviewed_proposal
from tests.helpers.screening_sqlite import seed_daily_bars

import baibai_engine.read_api as read_api
from baibai_engine.position.ledger import ExecutionEvent, ReservationEvent
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research.assessment import AssessmentRef
from baibai_engine.research.assessment_store import AssessmentStoreService
from baibai_engine.research.proposal import InvestmentProposal, proposal_draft_sha256
from baibai_engine.research.proposal_inputs import build_proposal_context
from baibai_engine.research.proposal_service import InvestmentProposalService


@pytest.mark.parametrize("part", ["assessment_id", "core_sha256", "review_id", "argument_id"])
def test_projection_resolves_an_exact_id_hash_review_pair(tmp_path, part):
    case = backend_case(tmp_path)
    reference = AssessmentRef(
        assessment_id=case.pair.assessment_id,
        core_sha256=case.pair.core_sha256,
        review_id=case.pair.review.review_id,
    )
    argument_id = case.pair.assessment_id
    if part == "argument_id":
        argument_id = assessment_id(90)
    else:
        reference = reference.model_copy(
            update={
                part: {
                    "assessment_id": assessment_id(90),
                    "core_sha256": "0" * 64,
                    "review_id": review_id(90),
                }[part]
            }
        )
    before = case.app.read_bytes()
    error = {
        "assessment_id": "unknown assessment",
        "core_sha256": "Assessment reference differs",
        "review_id": "Assessment reference differs",
        "argument_id": "assessment reference identity differs",
    }[part]
    with pytest.raises(ValueError, match=error):
        read_api.reviewed_assessment_projection(
            case.app, assessment_id=argument_id, assessment_ref=reference
        )
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("mutation", ["draft-hash", "item-index", "exact-reference", "buy-binding"])
def test_proposal_readers_reject_corrupt_saved_identity_and_reference_closure(tmp_path, mutation):
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    InvestmentProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        proposal
    )
    with closing(sqlite3.connect(case.app)) as connection, connection:
        if mutation == "item-index":
            connection.execute("UPDATE investment_proposal_item SET action='no_action'")
        elif mutation == "buy-binding":
            connection.execute("DELETE FROM position_decision_reference")
        else:
            connection.execute("DROP TRIGGER investment_proposal_immutable")
            if mutation == "draft-hash":
                connection.execute("UPDATE investment_proposal SET draft_sha256=?", ("0" * 64,))
            else:
                raw = proposal.payload()
                raw["items"][0]["assessment_ref"]["review_id"] = review_id(90)
                raw["research_scope"]["outcomes"][0]["assessment_ref"]["review_id"] = review_id(90)
                draft_hash = proposal_draft_sha256(InvestmentProposal.model_validate(raw))
                raw["review"]["draft_sha256"] = draft_hash
                connection.execute(
                    "UPDATE investment_proposal SET payload=?,draft_sha256=?",
                    (json.dumps(raw), draft_hash),
                )
    before = case.app.read_bytes()
    error = {
        "draft-hash": "stored proposal identity/hash differs",
        "item-index": "stored proposal item index differs",
        "exact-reference": "Assessment reference differs",
        "buy-binding": "stored proposal buy binding differs",
    }[mutation]
    for reader in (
        lambda: read_api.list_investment_proposal_payloads(case.app),
        lambda: read_api.investment_proposal_payload(case.app, proposal_id=proposal.proposal_id),
        lambda: read_api.preview_current_proposal_terms(
            case.app, proposal_id=proposal.proposal_id, sqlite_path=case.market, now=PUBLISHED
        ),
    ):
        with pytest.raises(ValueError, match=error):
            reader()
    assert case.app.read_bytes() == before


def test_invalid_latest_publication_is_not_hidden_by_a_valid_old_pair(tmp_path):
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    InvestmentProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        proposal
    )
    document, review = pair_payload(review_number=90)
    newer = AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(90), document, review, supersedes_id=case.pair.assessment_id
    )
    with closing(sqlite3.connect(case.app)) as connection, connection:
        connection.execute("DROP TRIGGER stock_assessment_immutable")
        connection.execute(
            "UPDATE stock_assessment SET payload='{}' WHERE assessment_id=?",
            (newer.assessment_id,),
        )
    before = case.app.read_bytes()
    for reader in (
        lambda: read_api.list_stock_assessment_publications(case.app, ticker="1234"),
        lambda: read_api.list_assessment_review_publications(case.app),
        lambda: read_api.stock_assessment_publication(case.app, assessment_id=newer.assessment_id),
    ):
        with pytest.raises(ValueError, match="要再評価"):
            reader()
    old = read_api.stock_assessment_publication(case.app, assessment_id=case.pair.assessment_id)
    assert old["assessment_id"] == case.pair.assessment_id
    saved = read_api.investment_proposal_payload(case.app, proposal_id=proposal.proposal_id)
    assert saved["items"][0]["assessment_projection"]["raw"] == case.pair.document.model_dump(
        mode="json"
    )
    assert case.app.read_bytes() == before


def test_original_held_assessment_projection_keeps_original_price_and_pair(tmp_path):
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    InvestmentProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        proposal
    )
    reservation = ReservationEvent.model_validate(
        {
            "type": "reservation",
            "event_id": "reported-order",
            "reservation_id": "reported-reservation",
            "order_id": "reported-broker-order",
            "occurred_at": "2026-10-15T09:00:00+09:00",
            "ticker": "1234",
            "side": "buy",
            "sector": "情報・通信業",
            "common_factors": ["domestic-demand"],
            "decision_reference": proposal.proposal_id,
            "quantity": 100,
            "price_guard_yen": "1000",
            "expires_at": "2026-10-15T15:30:00+09:00",
        }
    )
    execution = ExecutionEvent.model_validate(
        {
            "type": "execution",
            "event_id": "reported-fill",
            "execution_id": "reported-broker-fill",
            "occurred_at": "2026-10-15T09:01:00+09:00",
            "ticker": "1234",
            "side": "buy",
            "reservation_id": reservation.reservation_id,
            "decision_reference": proposal.proposal_id,
            "quantity": 100,
            "price_yen": "1000",
        }
    )
    current_time = PUBLISHED + timedelta(days=1)
    store = LedgerStoreService(case.app)
    ledger, head = store.load_with_head()
    replacement = ledger.model_copy(
        update={"events": (*ledger.events, reservation, execution), "as_of": current_time}
    )
    store.apply_document(expected_head=head, expected_document=ledger, replacement=replacement)
    seed_daily_bars(case.market, [("1234", "2026-10-15", 900.0, 1.0)])
    document, review = pair_payload(as_of="2026-10-15", review_number=90)
    document["valuation"]["base"]["terminal_value_per_share_yen"] = "1500"
    review["recalculated_projections"][0]["terminal_value_per_share_yen"] = "1500"
    update_review_hash(document, review)
    AssessmentStoreService(
        case.app, clock=lambda: PREPARED + timedelta(days=1)
    ).publish_reviewed_assessment(
        assessment_id(90), document, review, supersedes_id=case.pair.assessment_id
    )
    before = case.app.read_bytes()
    context = build_proposal_context(
        operation_id=case.operation.operation_id,
        proposal_id=proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=current_time,
    )
    original = context.inputs.positions[0].original_assessment_ref
    assert original == proposal.items[0].assessment_ref
    assert context.inputs.quotes[0].price_yen == 900
    projection = read_api.reviewed_assessment_projection(
        case.app, assessment_id=original.assessment_id, assessment_ref=original
    )
    assert projection["as_of"] == "2026-10-14"
    assert projection["original_price_yen"] == 1100
    assert projection["pmax_original_yen"] == 1000
    assert projection["raw"] == case.pair.document.model_dump(mode="json")
    saved = read_api.investment_proposal_payload(case.app, proposal_id=proposal.proposal_id)
    preview = read_api.preview_current_proposal_terms(
        case.app, proposal_id=proposal.proposal_id, sqlite_path=case.market, now=current_time
    )
    assert preview["status"] == "blocked"
    assert preview["items"][0]["saved_terms"] == proposal.items[0].terms.model_dump(mode="json")
    assert read_api.investment_proposal_payload(case.app, proposal_id=proposal.proposal_id) == saved
    assert case.app.read_bytes() == before


def test_projection_loads_and_validates_its_saved_pair_once(tmp_path, monkeypatch):
    from baibai_engine.read_api import research as research_read

    case = backend_case(tmp_path)
    original = research_read.load_reviewed_assessment
    loaded = []

    def load(connection, identifier):
        loaded.append(identifier)
        return original(connection, identifier)

    monkeypatch.setattr(research_read, "load_reviewed_assessment", load)
    before = case.app.read_bytes()
    projection = research_read.reviewed_assessment_projection(
        case.app, assessment_id=case.pair.assessment_id
    )
    assert loaded == [case.pair.assessment_id]
    assert projection["raw"] == case.pair.document.model_dump(mode="json")
    assert case.app.read_bytes() == before
