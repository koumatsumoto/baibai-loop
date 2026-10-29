"""保存評価・提案・人間の注文報告を介した資金再読と保存条件の独立性。"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.proposal import PUBLISHED, backend_case, bind_review

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.query import (
    get_investment_proposal,
    preview_current_terms,
)
from baibai_engine.investment_judgment.trade_proposal.scaffold import scaffold_proposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.portfolio.ledger.decision_references import resolve_buy_reference
from baibai_engine.portfolio.ledger.drafts import apply_draft, build_broker_fact_draft
from baibai_engine.portfolio.ledger.events import replay_events_through
from baibai_engine.portfolio.ledger.store import LedgerStoreService


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
    first_completed = None
    operation = case.operation
    for index, (ticker, quantity, cash) in enumerate(
        (("1234", 300, 500000), ("5678", 200, 200000))
    ):
        now = PUBLISHED + timedelta(minutes=index)
        if index:
            # A new explicit request follows the first proposal's completion and
            # human report. The already held/reserved ticker is not selected again.
            selected = seed_generated_selection(
                runs=case.runs, db=case.app, now=now, tickers=(ticker,), scope="specified_stocks"
            )
            operation = case.operations.start(
                session_kind="capital-allocation",
                as_of=selected.research_as_of,
                started_at=now,
                payload=OperationPayload(
                    checkpoint="人間報告後の新しい明示依頼",
                    artifacts=(selected.model_dump(mode="json"),),
                ),
            )
            operation = case.operations.record_assessment(
                operation.operation_id, pair.assessment_id, now=now
            )
            assert operation.operation_id != case.operation.operation_id
            assert case.operations.get(case.operation.operation_id) == first_completed
        proposal_id = f"investment-proposal-00000000-0000-4000-8000-{index + 7:012x}"
        raw = scaffold_proposal(
            operation_id=operation.operation_id,
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
        published = TradeProposalService(
            case.app, sqlite_path=case.market, clock=lambda instant=now: instant
        ).publish(proposal)
        assert store.load() == before
        if index == 0:
            first_proposal = published
        else:
            assert published.research_scope.operation_id == operation.operation_id
            assert [target.ticker for target in published.research_scope.targets] == ["5678"]
            assert published.inputs.active_reservations or published.inputs.positions
            if candidate_factors:
                assert published.portfolio_exposure.common_factors[0].key == "cycle"
            else:
                assert "5678" in published.portfolio_exposure.common_factor_unclassified_tickers
        operation = case.operations.record_proposal(
            operation.operation_id, published.proposal_id, now=now
        )
        completed = case.operations.complete(
            operation.operation_id,
            result="具体条件まで提案を完成した。",
            next="本人の採否・broker操作と報告を待つ。",
            completed_at=now,
        )
        assert completed.status == "completed"
        if index == 0:
            first_completed = completed
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
    assert first_completed is not None
    assert case.operations.active() is None
    assert case.operations.get(first_completed.operation_id) == first_completed
    assert [target.ticker for target in first_proposal.research_scope.targets] == ["1234", "5678"]
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
