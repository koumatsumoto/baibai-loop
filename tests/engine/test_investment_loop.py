"""保存評価・提案・人間の注文報告を介した資金再読と保存条件の独立性。"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import PUBLISHED, backend_case, bind_review

from baibai_engine.position.decision_references import resolve_buy_reference
from baibai_engine.position.drafts import apply_draft, build_broker_fact_draft
from baibai_engine.position.ledger import replay_events_through
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research.assessment_store import AssessmentStoreService
from baibai_engine.research.proposal_query import get_investment_proposal, preview_current_terms
from baibai_engine.research.proposal_scaffold import scaffold_proposal
from baibai_engine.research.proposal_service import InvestmentProposalService


@pytest.mark.parametrize("fill_first", [False, True])
@pytest.mark.parametrize("candidate_factors", [[], ["cycle"]])
def test_sequential_proposals_recollect_cash_without_changing_saved_scope_or_terms(
    tmp_path, fill_first, candidate_factors
):
    case = backend_case(tmp_path, cash_yen=500000, tickers=("1234", "5678"))
    second, review = pair_payload(ticker="5678", review_number=4)
    second["input_snapshot"]["common_factors"] = candidate_factors
    update_review_hash(second, review)
    pair = AssessmentStoreService(
        case.app, clock=lambda: datetime.fromisoformat("2026-10-14T16:06:00+09:00")
    ).publish_reviewed_assessment(assessment_id(3), second, review)
    case.operations.record_assessment(
        case.operation.operation_id, pair.assessment_id, now=PUBLISHED
    )
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=1000")
    store = LedgerStoreService(case.app)
    resolve = partial(resolve_buy_reference, db_path=case.app)
    documents = {case.pair.assessment_id: case.pair.document, pair.assessment_id: pair.document}
    first_proposal = None
    for index, (ticker, quantity, cash) in enumerate(
        (("1234", 300, 500000), ("5678", 200, 200000))
    ):
        now = PUBLISHED + timedelta(minutes=index)
        proposal_id = f"investment-proposal-00000000-0000-4000-8000-{index + 7:012x}"
        raw = scaffold_proposal(
            operation_id=case.operation.operation_id,
            proposal_id=proposal_id,
            db_path=case.app,
            sqlite_path=case.market,
            now=now,
        )
        raw.update(
            headline="合成の逐次提案",
            comparison="確認済みcashと保存評価を比較",
            forgone="既保有・予約の追加購入をしない",
        )
        for item in raw["items"]:
            item.pop("blockers")
            item.pop("resume_conditions")
            item["action"] = "buy" if item["ticker"] == ticker else "no_action"
            item["rationale"] = (
                "確認済みの資金内で条件を確定"
                if item["ticker"] == ticker
                else "今回は追加購入しない"
            )
            if item["ticker"] == ticker:
                item.update(
                    timing="at_current_limit",
                    terms={
                        "side": "buy",
                        "limit_price_yen": "1000",
                        "quantity": quantity,
                        "notional_yen": str(1000 * quantity),
                        "target_session": "2026-10-15",
                        "expires_at": "2026-10-15T15:30:00+09:00",
                        "valuation_as_of": "2026-10-14",
                        "maximum_entry_price_yen": "1000",
                        "price_grid_rule_id": "tse-common-conservative-20261014",
                        "conditions": ["仮説・資金・株式単位・注文状態に変化がないこと"],
                        "warnings": [],
                    },
                )
        raw["review"].update(
            reviewer_identity="synthetic-independent-reviewer", reviewed_at=now.isoformat()
        )
        proposal = bind_review(raw, documents)
        assert proposal.inputs.available_cash_yen == cash
        before = store.load()
        published = InvestmentProposalService(
            case.app, sqlite_path=case.market, clock=lambda instant=now: instant
        ).publish(proposal)
        assert store.load() == before
        if index == 0:
            first_proposal = published
        else:
            assert published.inputs.active_reservations or published.inputs.positions
            if candidate_factors:
                assert published.portfolio_exposure.common_factors[0].key == "cycle"
            else:
                assert "5678" in published.portfolio_exposure.common_factor_unclassified_tickers
        buy_item = next(item for item in published.items if item.ticker == ticker)
        assert buy_item.terms.notional_yen == 1000 * quantity
        draft, _ = build_broker_fact_draft(
            store,
            resolve,
            decision_reference=proposal_id,
            status="open",
            occurred_at=now,
            ticker=ticker,
            quantity=quantity,
            order_id="synthetic-order-" + ticker,
            sector="情報・通信業",
            common_factors=(),
            price_guard_yen=Decimal(1000),
            expires_at=buy_item.terms.expires_at,
            now=now,
        )
        assert draft is not None
        apply_draft(store, draft, human_confirmed=True)
        if index == 0 and fill_first:
            reservation = next(
                event
                for event in store.load().events
                if event.type == "reservation" and event.ticker == ticker
            )
            fill, _ = build_broker_fact_draft(
                store,
                resolve,
                decision_reference=proposal_id,
                status="filled",
                occurred_at=now,
                ticker=ticker,
                quantity=quantity,
                price_yen=Decimal(1000),
                reservation_id=reservation.reservation_id,
                now=now,
            )
            assert fill is not None
            apply_draft(store, fill, human_confirmed=True)
    assert first_proposal is not None
    state = replay_events_through(store.load().events, store.load().as_of)
    assert state.available_cash_yen == 0
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=1250")
    preview = preview_current_terms(
        proposal_id=first_proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=2),
    )
    first_row = next(row for row in preview.items if row["ticker"] == "1234")
    assert first_row["saved_terms"]["limit_price_yen"] == "1000"
    assert first_row["current_quote_calculation"]["current_price_above_maximum"] is True
    assert (
        get_investment_proposal(proposal_id=first_proposal.proposal_id, db_path=case.app)
        == first_proposal
    )
