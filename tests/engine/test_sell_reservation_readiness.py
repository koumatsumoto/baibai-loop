from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
from functools import partial

import pytest
from tests.helpers.proposal import PUBLISHED, backend_case, bind_review, reviewed_proposal

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalConflictError,
    ProposalError,
    order_readiness,
)
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.query import (
    get_investment_proposal,
    preview_current_terms,
)
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.portfolio.ledger.decision_references import resolve_buy_reference
from baibai_engine.portfolio.ledger.drafts import (
    apply_draft,
    build_broker_fact_draft,
    build_sell_execution_draft,
)
from baibai_engine.portfolio.ledger.events import replay_events_through
from baibai_engine.portfolio.ledger.store import LedgerStoreService

SELL_TERMS = {
    "side": "sell",
    "limit_price_yen": "1100",
    "quantity": 100,
    "notional_yen": "110000",
    "target_session": "2026-10-15",
    "valuation_as_of": "2026-10-14",
    "maximum_entry_price_yen": None,
    "price_grid_rule_id": "tse-common-conservative-20261014",
    "conditions": ["実注文前に保有数量と既存注文の処理を確認する。"],
    "warnings": [],
}
RESERVATION_REASON = {
    "code": "active_reservation_exists",
    "message": "同銘柄の既存注文の処理確認が必要です。",
}


@pytest.mark.parametrize("side", ["sell", "buy"])
def test_same_ticker_order_blocks_new_sell_terms_but_preserves_economic_sell(tmp_path, side):
    case = backend_case(tmp_path, holding=True, broken=True)
    store = LedgerStoreService(case.app)
    opened, _ = build_broker_fact_draft(
        store,
        partial(resolve_buy_reference, db_path=case.app),
        decision_reference=None if side == "buy" else "synthetic-working-sell",
        direct_fact=side == "buy",
        status="open",
        side=side,
        occurred_at=PUBLISHED - timedelta(minutes=11),
        ticker="1234",
        quantity=100,
        price_guard_yen=Decimal(800),
        sector="情報・通信業",
        common_factors=("domestic-demand",),
        reservation_id="synthetic-working-order",
        now=PUBLISHED,
    )
    assert opened is not None
    apply_draft(store, opened, human_confirmed=True)
    raw = reviewed_proposal(case, action="sell").payload()
    raw["items"][0].update(terms=dict(SELL_TERMS), terms_unavailable_reasons=[])
    concrete = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    assert case.pair.document.investment_case.status == "broken"
    assert case.pair.review.primary_source_check == "verified"
    assert concrete.inputs.positions[0].quantity_basis_confirmed
    assert concrete.inputs.quotes[0].price_yen == Decimal(1100)
    assert concrete.inputs.active_reservations[0].reservation_id == "synthetic-working-order"
    assert concrete.inputs.available_cash_yen == (130000 if side == "buy" else 210000)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    ledger_before = store.load_with_head()
    before = case.app.read_bytes()
    validation = service.check(concrete)
    assert [reason.code for reason in validation.blockers] == ["active_reservation_exists"]
    assert not validation.valid
    with pytest.raises(ProposalError, match="既存注文の処理確認が必要"):
        service.publish(concrete)
    assert case.app.read_bytes() == before

    raw["items"][0].update(terms=None, terms_unavailable_reasons=[RESERVATION_REASON])
    incomplete = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    assert service.check(incomplete).valid
    published = service.publish(incomplete)
    assert published.items[0].action == "sell"
    assert published.items[0].reason_code == "economic_invalidation"
    assert published.items[0].position.quantity == 100
    assert order_readiness(published.items[0]) == "needs_terms"
    assert store.load_with_head() == ledger_before
    with closing(sqlite3.connect(case.app)) as connection:
        bindings = connection.execute("SELECT * FROM position_decision_reference").fetchall()
    assert len(bindings) == (1 if side == "buy" else 0)


@pytest.mark.parametrize(
    ("side", "ticker", "blocked"),
    [("sell", "1234", True), ("buy", "1234", True), ("buy", "5678", False)],
)
def test_current_order_only_blocks_preview_and_keeps_saved_ready_proposal(
    tmp_path, side, ticker, blocked
):
    case = backend_case(tmp_path, holding=True, broken=True)
    raw = reviewed_proposal(case, action="sell").payload()
    raw["items"][0].update(terms=dict(SELL_TERMS), terms_unavailable_reasons=[])
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    # A normal quote, confirmed units, classification and session permit the full sale.
    assert service.check(proposal).valid
    published = service.publish(proposal)
    assert order_readiness(published.items[0]) == "ready"
    store = LedgerStoreService(case.app)
    now = PUBLISHED + timedelta(minutes=2)
    opened, _ = build_broker_fact_draft(
        store,
        partial(resolve_buy_reference, db_path=case.app),
        decision_reference=None if side == "buy" else "synthetic-working-sell",
        direct_fact=side == "buy",
        status="open",
        side=side,
        occurred_at=now - timedelta(minutes=1),
        ticker=ticker,
        quantity=100,
        price_guard_yen=Decimal(800),
        sector="情報・通信業",
        common_factors=("domestic-demand",),
        now=now,
    )
    assert opened is not None
    apply_draft(store, opened, human_confirmed=True)
    before = case.app.read_bytes()
    preview = preview_current_terms(
        proposal_id=published.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=now,
    )
    assert get_investment_proposal(proposal_id=published.proposal_id, db_path=case.app) == published
    current_service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: now)
    assert current_service.publish(published) == published
    assert case.app.read_bytes() == before
    assert preview.status == ("blocked" if blocked else "changed")
    row = preview.items[0]
    assert row["saved_action"] == row["current_quote_calculation"]["action"] == "sell"
    assert row["saved_order_readiness"] == "ready"
    assert row["saved_terms"] == published.items[0].terms.model_dump(mode="json")
    expected = ["active_reservation_exists"] if blocked else []
    assert [reason["code"] for reason in row["blockers"]] == expected
    assert [
        reason["code"] for reason in row["saved_limit_revalidation"]["hard_blockers"]
    ] == expected
    assert preview.current_inputs.active_reservations[0].ticker == ticker

    if not blocked:
        raw = published.payload()
        raw.update(
            proposal_id="investment-proposal-00000000-0000-4000-8000-000000000004",
            prepared_at=now.isoformat(),
            published_at=now.isoformat(),
            inputs=preview.current_inputs.model_dump(mode="json"),
        )
        raw["review"]["reviewed_at"] = now.isoformat()
        fresh = bind_review(raw, {case.pair.assessment_id: case.pair.document})
        ledger_before = store.load_with_head()
        assert current_service.check(fresh).valid
        assert order_readiness(current_service.publish(fresh).items[0]) == "ready"
        assert store.load_with_head() == ledger_before


def test_partial_sell_stays_active_past_expiry_until_reported_cancel_and_new_inputs(tmp_path):
    case = backend_case(tmp_path, holding=True, broken=True)
    store = LedgerStoreService(case.app)
    resolve = partial(resolve_buy_reference, db_path=case.app)
    bought, _ = build_broker_fact_draft(
        store,
        resolve,
        decision_reference=None,
        direct_fact=True,
        status="filled",
        occurred_at=PUBLISHED - timedelta(minutes=15),
        ticker="1234",
        quantity=200,
        price_yen=Decimal(800),
        sector="情報・通信業",
        common_factors=("domestic-demand",),
        now=PUBLISHED,
    )
    assert bought is not None
    apply_draft(store, bought, human_confirmed=True)
    opened, _ = build_broker_fact_draft(
        store,
        resolve,
        decision_reference="synthetic-working-sell",
        status="open",
        side="sell",
        occurred_at=PUBLISHED - timedelta(minutes=14),
        ticker="1234",
        quantity=200,
        price_guard_yen=Decimal(1100),
        sector="情報・通信業",
        common_factors=("domestic-demand",),
        reservation_id="synthetic-working-order",
        expires_at=PUBLISHED - timedelta(minutes=8),
        now=PUBLISHED,
    )
    assert opened is not None
    apply_draft(store, opened, human_confirmed=True)
    filled = build_sell_execution_draft(
        store,
        occurred_at=PUBLISHED - timedelta(minutes=11),
        ticker="1234",
        quantity=100,
        price_yen=Decimal(1100),
        reservation_id="synthetic-working-order",
        execution_id="synthetic-partial-sell",
    )
    assert filled is not None
    apply_draft(store, filled, human_confirmed=True)
    ledger_before = store.load_with_head()
    state = replay_events_through(ledger_before[0].events, PUBLISHED)
    reservation = state.active_reservations["synthetic-working-order"]
    assert reservation.remaining_quantity == 100
    assert reservation.expires_at < PUBLISHED
    assert state.available_cash_yen == 160000
    raw = reviewed_proposal(case, action="sell").payload()
    # Sell still means all 200 held shares, including the 100 on the existing order.
    raw["items"][0].update(
        terms=dict(SELL_TERMS, quantity=200, notional_yen="220000"),
        terms_unavailable_reasons=[],
    )
    concrete = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    assert concrete.items[0].position.quantity == 200
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert [reason.code for reason in service.check(concrete).blockers] == [
        "active_reservation_exists"
    ]
    raw["items"][0].update(terms=None, terms_unavailable_reasons=[RESERVATION_REASON])
    incomplete = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    published = service.publish(incomplete)
    assert order_readiness(published.items[0]) == "needs_terms"
    assert store.load_with_head() == ledger_before

    now = PUBLISHED + timedelta(minutes=2)
    cancelled, _ = build_broker_fact_draft(
        store,
        resolve,
        decision_reference="synthetic-working-sell",
        status="cancelled",
        side="sell",
        reservation_id="synthetic-working-order",
        occurred_at=now - timedelta(minutes=1),
        now=now,
    )
    assert cancelled is not None
    apply_draft(store, cancelled, human_confirmed=True)
    current_service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: now)
    with pytest.raises(ProposalConflictError, match="input facts changed"):
        current_service.check(concrete)
    context = build_proposal_context(
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=now,
    )
    assert context.inputs.active_reservations == ()
    assert context.inputs.available_cash_yen == Decimal(160000)
    raw = concrete.payload()
    raw.update(
        proposal_id="investment-proposal-00000000-0000-4000-8000-000000000004",
        prepared_at=now.isoformat(),
        published_at=now.isoformat(),
        inputs=context.inputs.model_dump(mode="json"),
    )
    raw["review"]["reviewed_at"] = now.isoformat()
    fresh = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    ledger_after_cancel = store.load_with_head()
    assert current_service.check(fresh).valid
    assert order_readiness(current_service.publish(fresh).items[0]) == "ready"
    assert store.load_with_head() == ledger_after_cancel
    assert current_service.publish(published) == published
    preview = preview_current_terms(
        proposal_id=published.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=now,
    )
    assert [reason["code"] for reason in preview.items[0]["blockers"]] == [
        "order_terms_unconfirmed"
    ]
    assert preview.items[0]["saved_order_readiness"] == "needs_terms"
