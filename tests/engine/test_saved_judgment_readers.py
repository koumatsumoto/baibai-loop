"""正確な保存pairと当初評価を読み、破損版を旧版で隠さない。"""

import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, review_id, update_review_hash
from tests.helpers.proposal import PREPARED, PUBLISHED, backend_case, reviewed_proposal
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment.scenario import maximum_entry_price
from baibai_engine.investment_judgment.stock_assessment.store import (
    AssessmentStoreService,
    load_latest_reviewed_assessment,
    load_reviewed_assessment,
    require_assessment_ref,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    TradeProposal,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.query import (
    get_investment_proposal,
    load_latest_investment_proposal,
    preview_current_terms,
)
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.portfolio.ledger.events import ExecutionEvent, ReservationEvent
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_engine.storage.sqlite.application.read import connect_read_only


@pytest.mark.parametrize("part", ["assessment_id", "core_sha256", "review_id"])
def test_owner_resolves_an_exact_id_hash_review_pair(tmp_path, part):
    case = backend_case(tmp_path)
    reference = AssessmentRef(
        assessment_id=case.pair.assessment_id,
        core_sha256=case.pair.core_sha256,
        review_id=case.pair.review.review_id,
    )
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
    }[part]
    with closing(connect_read_only(case.app)) as connection, pytest.raises(ValueError, match=error):
        require_assessment_ref(connection, reference)
    assert case.app.read_bytes() == before


@pytest.mark.parametrize(
    "mutation", ["draft-hash", "item-index", "exact-reference", "buy-binding", "publication-time"]
)
def test_proposal_readers_reject_corrupt_saved_identity_and_reference_closure(tmp_path, mutation):
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    proposal = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
    with closing(sqlite3.connect(case.app)) as connection, connection:
        if mutation == "item-index":
            connection.execute("UPDATE investment_proposal_item SET action='no_action'")
        elif mutation == "buy-binding":
            connection.execute("DELETE FROM position_decision_reference")
        else:
            connection.execute("DROP TRIGGER investment_proposal_immutable")
            if mutation == "draft-hash":
                connection.execute("UPDATE investment_proposal SET draft_sha256=?", ("0" * 64,))
            elif mutation == "publication-time":
                raw = proposal.payload()
                raw["published_at"] = PREPARED.isoformat()
                connection.execute(
                    "UPDATE investment_proposal SET payload=?,published_at=?",
                    (json.dumps(raw), raw["published_at"]),
                )
            else:
                raw = proposal.payload()
                raw["items"][0]["assessment_ref"]["review_id"] = review_id(90)
                raw["research_scope"]["outcomes"][0]["assessment_ref"]["review_id"] = review_id(90)
                draft_hash = proposal_draft_sha256(TradeProposal.model_validate(raw))
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
        "publication-time": "prepared_at <= reviewed_at <= published_at",
    }[mutation]
    for reader in (
        lambda: get_investment_proposal(db_path=case.app, proposal_id=proposal.proposal_id),
        lambda: preview_current_terms(
            db_path=case.app,
            proposal_id=proposal.proposal_id,
            sqlite_path=case.market,
            now=PUBLISHED,
        ),
    ):
        with pytest.raises(ValueError, match=error):
            reader()
    with (
        closing(connect_read_only(case.app)) as connection,
        pytest.raises(ValueError, match=error),
    ):
        load_latest_investment_proposal(connection)
    assert case.app.read_bytes() == before


def test_invalid_latest_publication_is_not_hidden_by_a_valid_old_pair(tmp_path):
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    proposal = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
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
    with closing(connect_read_only(case.app)) as connection:
        with pytest.raises(ValueError, match="要再評価"):
            load_latest_reviewed_assessment(connection, "1234")
        with pytest.raises(ValueError, match="要再評価"):
            load_reviewed_assessment(connection, newer.assessment_id)
        old = load_reviewed_assessment(connection, case.pair.assessment_id)
    assert old == case.pair
    saved = get_investment_proposal(db_path=case.app, proposal_id=proposal.proposal_id)
    assert saved == proposal
    assert case.app.read_bytes() == before


def test_original_held_assessment_keeps_original_price_and_pair(tmp_path):
    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
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
    with closing(connect_read_only(case.app)) as connection:
        original_pair = require_assessment_ref(connection, original)
    valuation = original_pair.document.valuation
    price_fact = next(
        f
        for f in original_pair.document.input_snapshot.facts
        if f.fact_id == valuation.market_price_fact_id
    )
    assert original_pair.document.input_snapshot.as_of.isoformat() == "2026-10-14"
    assert Decimal(str(price_fact.value)) == 1100
    assert (
        maximum_entry_price(
            valuation.base,
            horizon_months=valuation.horizon_months,
            required_annual_return_pct=valuation.required_annual_return_pct,
        )
        == 1000
    )
    assert original_pair.document == case.pair.document
    saved = get_investment_proposal(db_path=case.app, proposal_id=proposal.proposal_id)
    preview = preview_current_terms(
        db_path=case.app,
        proposal_id=proposal.proposal_id,
        sqlite_path=case.market,
        now=current_time,
    )
    assert preview.status == "blocked"
    assert preview.items[0]["saved_terms"] == proposal.items[0].terms.model_dump(mode="json")
    assert get_investment_proposal(db_path=case.app, proposal_id=proposal.proposal_id) == saved
    assert case.app.read_bytes() == before
