"""F09/F10/F17/F23: fixture DB exercises the composed investment loop."""

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from tests.engine.test_reviewed_thesis_v4 import NOW
from tests.helpers.db_seed import seed_ledger
from tests.helpers.research_v4 import pair_payload

from baibai_engine.appdb.write import connect_rw
from baibai_engine.operation.models import OperationPayload
from baibai_engine.operation.service import OperationService
from baibai_engine.position.drafts import apply_draft, build_sell_execution_draft
from baibai_engine.position.ledger import PortfolioLedgerDocument
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.research.broker_fact_service import build_broker_fact_draft
from baibai_engine.research.capital_allocation import (
    CapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)
from baibai_engine.research.capital_allocation_scaffold import scaffold_capital_allocation
from baibai_engine.research.capital_allocation_service import CapitalAllocationAssessmentService
from baibai_engine.research.planning import plan_limit
from baibai_engine.research.position_review_service import PositionReviewService
from baibai_engine.research.thesis import ThesisDocument, thesis_core_hash
from baibai_engine.research.thesis_store import ThesisStoreService


def test_sequential_allocation_uses_updated_cash_and_one_research_set(tmp_path):
    db, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    initial = PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 2,
            "portfolio_scope": "repository_only",
            "as_of": NOW.isoformat(),
            "events": [
                {
                    "event_id": "opening",
                    "type": "opening_balance",
                    "occurred_at": (NOW - timedelta(days=1)).isoformat(),
                    "amount_yen": 500000,
                }
            ],
            "market_prices": [],
        }
    )
    seed_ledger(db, initial)
    with sqlite3.connect(market) as connection:
        connection.execute("PRAGMA user_version=25")
        connection.execute(
            "CREATE TABLE jquants_daily_bars(ticker TEXT,traded_at TEXT,close REAL,adjustment_factor REAL)"
        )
        connection.executemany(
            "INSERT INTO jquants_daily_bars VALUES (?,'2026-09-04',1000,1)", [("1234",), ("5678",)]
        )
    pairs = {}
    for ticker in ("1234", "5678"):
        thesis, review = pair_payload()
        thesis["input_snapshot"]["ticker"] = ticker
        thesis["input_snapshot"]["sources"][0]["ticker"] = ticker
        review["review_id"] = "review-" + ticker
        review["reviewed_thesis_sha256"] = thesis_core_hash(ThesisDocument.model_validate(thesis))
        pairs[ticker] = ThesisStoreService(db, clock=lambda: NOW).publish_reviewed_thesis(
            "thesis-" + ticker, thesis, review
        )
    with connect_rw(db) as connection:
        connection.execute(
            "INSERT INTO research_triage VALUES (?,?,?,?,?,?)",
            (
                "triage",
                "set",
                "run",
                "2026-09-07",
                NOW.isoformat(),
                json.dumps(
                    {
                        "as_of": "2026-09-07",
                        "entries": [
                            {
                                "ticker": ticker,
                                "decision": "research",
                                "candidate_snapshot": {
                                    "analysis": {"identity_liquidity": {"avg_turnover_oku": 0.01}}
                                },
                            }
                            for ticker in pairs
                        ],
                    }
                ),
            ),
        )
    operation = OperationService(db).start(
        session_kind="capital-allocation",
        as_of=NOW.date(),
        started_at=NOW,
        payload=OperationPayload(
            checkpoint="human selected",
            artifacts=({"kind": "research_triage", "ref": "triage", "research_set": list(pairs)},),
        ),
    )
    service = CapitalAllocationAssessmentService(db, sqlite_path=market, clock=lambda: NOW)
    first_id = "capital-allocation-assessment-20260907-first"
    final_id = "capital-allocation-assessment-20260907-second"
    for ticker, assessment_id, expected_quantity in (
        ("1234", first_id, 300),
        ("5678", final_id, 200),
    ):
        raw = scaffold_capital_allocation(
            db_path=db,
            capital_allocation_assessment_id=assessment_id,
            as_of=NOW.date(),
            research_triage_id="triage",
            thesis_ids=[pair.thesis_id for pair in pairs.values()],
            published_at=NOW,
        )
        raw.update(
            result="allocate",
            headline="cashより価値がある",
            comparison="期間とDownsideを比較",
            forgone="先行予約は追加配分しない",
        )
        for item in raw["alternatives"]:
            item["disposition"] = "allocate" if item["ticker"] == ticker else "decline"
            item["rationale"] = "比較して配分" if item["ticker"] == ticker else "今回は配分しない"
        assessment = CapitalAllocationAssessment.model_validate(raw)
        assessment = assessment.model_copy(
            update={
                "review": assessment.review.model_copy(
                    update={"draft_sha256": capital_allocation_draft_sha256(assessment)}
                )
            }
        )
        # A fresh publication may not reuse yesterday's formal basis.
        with pytest.raises(ValueError, match="basis must be today"):
            CapitalAllocationAssessmentService(
                db, sqlite_path=market, clock=lambda: NOW + timedelta(days=1)
            ).publish(assessment)
        service.publish(assessment)
        plan = plan_limit(
            capital_allocation_assessment_id=assessment_id,
            db_path=db,
            sqlite_path=market,
            target_session=NOW.date(),
            budget_min_yen=200000,
            budget_max_yen=300000,
            now=NOW,
        )
        assert "adv_participation_exceeds_warning" in plan["warnings"]
        assert plan["liquidity_context"]["adv_yen"] == 1000000
        assert plan["quantity"] == expected_quantity
        assert plan["available_cash_yen"] == (500000 if ticker == "1234" else 200000)
        draft, _ = build_broker_fact_draft(
            LedgerStoreService(db),
            service,
            decision_reference=assessment_id,
            status="open",
            occurred_at=NOW,
            ticker=ticker,
            quantity=expected_quantity,
            order_id="order-" + ticker,
            sector="機械",
            price_guard_yen=Decimal(1000),
            expires_at=NOW + timedelta(days=1),
            ordered_at=NOW,
            now=NOW,
        )
        assert draft is not None
        apply_draft(LedgerStoreService(db), draft, human_confirmed=True)
    OperationService(db).complete(
        operation.operation_id,
        OperationPayload(
            checkpoint="final",
            artifacts=(
                *operation.payload.artifacts,
                {"kind": "capital_allocation_assessment", "ref": final_id},
            ),
            canonical_refs=("triage", final_id),
            result="approved",
            next="next trigger",
            human_confirmation={"request": "confirm", "result": "approve"},
        ),
        completed_at=NOW,
    )
    assert OperationService(db).active() is None
    assert (
        CapitalAllocationAssessmentService(
            db, sqlite_path=market, clock=lambda: NOW + timedelta(days=1)
        ).publish(assessment)
        == assessment
    )
    ledger_service = LedgerStoreService(db)
    reservation = next(
        event
        for event in ledger_service.load().events
        if event.type == "reservation" and event.ticker == "1234"
    )
    fill, _ = build_broker_fact_draft(
        ledger_service,
        service,
        decision_reference=first_id,
        status="filled",
        occurred_at=NOW,
        ticker="1234",
        quantity=300,
        price_yen=Decimal(1000),
        reservation_id=reservation.reservation_id,
        now=NOW,
    )
    apply_draft(ledger_service, fill, human_confirmed=True)
    sell = build_sell_execution_draft(
        ledger_service, occurred_at=NOW, ticker="1234", quantity=300, price_yen=Decimal(1000)
    )
    apply_draft(ledger_service, sell, human_confirmed=True)
    old_plan = plan_limit(
        capital_allocation_assessment_id=first_id,
        db_path=db,
        sqlite_path=market,
        target_session=NOW.date(),
        budget_min_yen=200000,
        budget_max_yen=300000,
        now=NOW,
    )
    assert old_plan["quantity"] == 0
    assert "assessment_already_executed" in old_plan["defer_reasons"]
    # Null publication still requires confirmation before opening or creating a database.
    review_service = PositionReviewService(tmp_path / "absent.sqlite")
    with pytest.raises(ValueError, match="confirmation"):
        review_service.publish(None, confirmed=False)
