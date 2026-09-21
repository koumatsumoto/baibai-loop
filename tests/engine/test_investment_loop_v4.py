"""F09/F10/F17/F23: fixture DB exercises the composed investment loop."""

import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tests.engine.test_reviewed_thesis_v4 import NOW as EVENING
from tests.helpers.db_seed import seed_ledger
from tests.helpers.research_v4 import pair_payload

from baibai_engine.appdb.write import connect_rw
from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION
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


@pytest.mark.parametrize(
    ("NOW", "fill_first"), [(EVENING, False), (EVENING.replace(hour=10), True)]
)
@pytest.mark.parametrize("candidate_factors", [[], ["cycle"]])
def test_sequential_allocation_uses_updated_cash_and_one_research_set(
    tmp_path, NOW, fill_first, candidate_factors
):
    target = (NOW + timedelta(days=1)).date() if NOW.hour >= 15 else NOW.date()
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
        connection.execute(f"PRAGMA user_version={SQLITE_SCHEMA_VERSION}")
        connection.execute(
            "CREATE TABLE jquants_daily_bars(ticker TEXT,traded_at TEXT,close REAL,adjustment_factor REAL)"
        )
        connection.executemany(
            "INSERT INTO jquants_daily_bars VALUES (?,'2026-09-04',1000,1)", [("1234",), ("5678",)]
        )
        # Intraday raw closes are unavailable, but today's unchanged unit evidence is present.
        connection.executemany(
            "INSERT INTO jquants_daily_bars VALUES (?,'2026-09-07',NULL,1)",
            [("1234",), ("5678",)] if fill_first else [],
        )
        connection.execute("CREATE TABLE jquants_market_calendar(day TEXT,is_business_day INT)")
        connection.executemany(
            "INSERT INTO jquants_market_calendar VALUES (?,1)", [("2026-09-07",), ("2026-09-08",)]
        )
    pairs = {}
    for ticker in ("1234", "5678"):
        thesis, review = pair_payload()
        thesis["input_snapshot"]["ticker"] = ticker
        thesis["input_snapshot"]["common_factors"] = candidate_factors
        thesis["input_snapshot"]["sources"][0]["ticker"] = ticker
        thesis["input_snapshot"]["sources"][0]["retrieved_at"] = (
            NOW - timedelta(hours=3)
        ).isoformat()
        thesis["judgment"]["proposed_at"] = (NOW - timedelta(hours=2)).isoformat()
        review["reviewed_at"] = (NOW - timedelta(hours=1)).isoformat()
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
        if ticker == "1234":
            for review_id in ("wrong-review", None):
                for disposition in ("decline", "allocate"):
                    alternative = assessment.alternatives[0].model_copy(
                        update={"disposition": disposition, "thesis_review_id": review_id}
                    )
                    probe = assessment.model_copy(
                        update={
                            "capital_allocation_assessment_id": first_id
                            + f"-binding-{disposition}-{review_id}",
                            "result": "no_allocation" if disposition == "decline" else "allocate",
                            "alternatives": (alternative, assessment.alternatives[1]),
                        }
                    )
                    probe = probe.model_copy(
                        update={
                            "review": probe.review.model_copy(
                                update={"draft_sha256": capital_allocation_draft_sha256(probe)}
                            )
                        }
                    )
                    if disposition == "decline" and review_id is None:
                        assert service.publish(probe).alternatives[0].thesis_review_id is None
                    else:
                        with pytest.raises(ValueError, match=r"[Rr]eview"):
                            service.publish(probe)
                        with connect_rw(db) as connection:
                            assert (
                                connection.execute(
                                    "SELECT 1 FROM capital_allocation_assessment WHERE "
                                    "capital_allocation_assessment_id = ?",
                                    (probe.capital_allocation_assessment_id,),
                                ).fetchone()
                                is None
                            )
        service.publish(assessment)
        plan = plan_limit(
            capital_allocation_assessment_id=assessment_id,
            db_path=db,
            sqlite_path=market,
            target_session=target,
            budget_min_yen=200000,
            budget_max_yen=300000,
            now=NOW,
        )
        assert "adv_participation_exceeds_warning" in plan["warnings"]
        assert plan["liquidity_context"]["adv_yen"] == 1000000
        assert plan["status"] == "planned_limit"
        assert plan["current_price_projection"]["base"]["total_return_pct"] == 23.4
        assert plan["current_price_projection"]["downside"]["annualized_return_pct"] == -40
        unclassified = ([] if ticker == "1234" else ["1234"]) + (
            [] if candidate_factors else [ticker]
        )
        assert plan["portfolio_exposure"]["common_factor_unclassified_tickers"] == unclassified
        assert ("portfolio_exposure_common_factor_coverage_incomplete" in plan["warnings"]) == bool(
            unclassified
        )
        if candidate_factors:
            assert plan["portfolio_exposure"]["common_factors"][0]["key"] == "cycle"
        else:
            assert plan["portfolio_exposure"]["common_factors"] == []
        assert datetime.fromisoformat(plan["expires_at"]) > NOW
        assert plan["judgment_as_of"] == "2026-09-07"
        assert plan["target_session"] == target.isoformat()
        assert plan["quantity"] == expected_quantity
        assert plan["available_cash_yen"] == (500000 if ticker == "1234" else 200000)
        if ticker == "5678" and fill_first:
            assert plan["portfolio_exposure"]["total_capital_yen"] == 500000
            assert "prospective_ticker_concentration_exceeds_warning:5678" in plan["warnings"]
            assert "concentration_and_dry_powder_unassessed" not in plan["warnings"]
            assert LedgerStoreService(db).load().market_prices == ()
        with sqlite3.connect(market) as connection:
            connection.execute(
                "UPDATE jquants_daily_bars SET close=1250 WHERE ticker=? AND traded_at='2026-09-04'",
                (ticker,),
            )
        higher_quote = plan_limit(
            capital_allocation_assessment_id=assessment_id,
            db_path=db,
            sqlite_path=market,
            target_session=target,
            budget_min_yen=200000,
            budget_max_yen=300000,
            now=NOW,
        )
        assert higher_quote["status"] == "defer"
        assert "price_above_pmax" in higher_quote["defer_reasons"]
        assert higher_quote["current_price_projection"]["base"]["annualized_return_pct"] == -1.28
        assert higher_quote["current_price_projection"]["downside"]["total_return_pct"] == -52
        with sqlite3.connect(market) as connection:
            connection.execute(
                "UPDATE jquants_daily_bars SET close=1000 WHERE ticker=? AND traded_at='2026-09-04'",
                (ticker,),
            )
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
            expires_at=datetime.fromisoformat(plan["expires_at"]),
            ordered_at=NOW,
            now=NOW,
        )
        assert draft is not None
        apply_draft(LedgerStoreService(db), draft, human_confirmed=True)
        if ticker == "1234" and fill_first:
            reservation_id = next(
                event.reservation_id
                for event in draft.replacement.events
                if event.type == "reservation" and event.ticker == ticker
            )
            fill, _ = build_broker_fact_draft(
                LedgerStoreService(db),
                service,
                decision_reference=first_id,
                status="filled",
                occurred_at=NOW,
                ticker=ticker,
                quantity=300,
                price_yen=Decimal(1000),
                reservation_id=reservation_id,
                now=NOW,
            )
            apply_draft(LedgerStoreService(db), fill, human_confirmed=True)
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
    if not fill_first:
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
        target_session=target,
        budget_min_yen=200000,
        budget_max_yen=300000,
        now=NOW,
    )
    if NOW.hour >= 15:
        expired = plan_limit(
            capital_allocation_assessment_id=final_id,
            db_path=db,
            sqlite_path=market,
            target_session=NOW.date(),
            budget_min_yen=200000,
            budget_max_yen=300000,
            now=NOW,
        )
        assert expired["status"] == "defer"
        assert expired["expires_at"] is None
        assert "target_session_not_next_available" in expired["defer_reasons"]
    assert old_plan["quantity"] == 0
    assert "assessment_already_executed" in old_plan["defer_reasons"]
    # Null publication still requires confirmation before opening or creating a database.
    review_service = PositionReviewService(tmp_path / "absent.sqlite")
    with pytest.raises(ValueError, match="confirmation"):
        review_service.publish(None, confirmed=False)
