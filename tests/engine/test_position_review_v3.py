"""F12-F19/F23: current holding judgment and reported facts on a legacy ledger."""

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.research_v4 import pair_payload

from baibai_engine.operation.models import OperationPayload
from baibai_engine.operation.service import OperationService
from baibai_engine.position.drafts import apply_draft, build_sell_execution_draft
from baibai_engine.position.ledger import ContributionEvent, replay_events_through
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.read_api import list_position_review_publications
from baibai_engine.research.position_review import RemainingReward
from baibai_engine.research.position_review_service import PositionReviewService
from baibai_engine.research.thesis import ThesisDocument, thesis_core_hash
from baibai_engine.research.thesis_store import ResearchConflictError, ThesisStoreService

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")


@pytest.fixture
def holding_case(tmp_path):
    db, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    # Trade/income/cost/tax facts are preserved, with no portfolio price prerequisite.
    ledger = ledger.model_copy(update={"market_prices": ()})
    seed_ledger(db, ledger)
    with sqlite3.connect(market) as connection:
        connection.execute("PRAGMA user_version=25")
        connection.execute(
            "CREATE TABLE jquants_daily_bars(ticker TEXT,traded_at TEXT,close REAL,adjustment_factor REAL)"
        )
        connection.executemany(
            "INSERT INTO jquants_daily_bars VALUES ('2331',?,1000,1)",
            [("2026-06-03",), ("2026-07-01",), ("2026-09-04",)],
        )
    thesis, review = pair_payload(ticker="2331")
    ThesisStoreService(db, clock=lambda: NOW).publish_reviewed_thesis("current", thesis, review)
    return db, market, ledger


@pytest.mark.parametrize(
    ("reward", "action"), [("sufficient", "hold"), ("insufficient", "exit"), ("uncertain", None)]
)
def test_holding_judgment_preserves_author_and_allows_active_capital_research(
    holding_case, reward, action
):
    db, market, original = holding_case
    operation = OperationService(db).start(
        session_kind="capital-allocation",
        as_of=NOW.date(),
        started_at=NOW,
        payload=OperationPayload(
            checkpoint="research",
            artifacts=({"kind": "research_triage", "ref": "unrelated", "research_set": ["5678"]},),
        ),
    )
    service = PositionReviewService(db, sqlite_path=market, clock=lambda: NOW)
    draft = service.build(thesis_id="current", position_id="2331")
    reason = "受取済配当3300円と権利確定分は将来増分に含めず、原評価の期間から経済的見返りを確認"
    draft = draft.model_copy(
        update={"remaining_reward": RemainingReward(status=reward, reason=reason), "action": action}
    )
    # An unrelated contribution must not invalidate the author's economic judgment.
    ledger = LedgerStoreService(db)
    before, head = ledger.load_with_head()
    contribution = ContributionEvent.model_validate(
        {
            "type": "contribution",
            "event_id": "confirmed",
            "occurred_at": (NOW - timedelta(minutes=1)).isoformat(),
            "amount_yen": 10000,
        }
    )
    ledger.apply_document(
        expected_head=head,
        expected_document=before,
        replacement=before.model_copy(
            update={"as_of": NOW, "events": (*before.events, contribution)}
        ),
    )
    evaluation = service.check(draft)
    assert evaluation.action == action
    assert service.publish(draft, confirmed=True).remaining_reward.reason == reason
    assert service.publish(draft, confirmed=True) == draft
    assert list_position_review_publications(db)[0]["payload"]["action"] == action
    assert OperationService(db).active() == operation
    assert ledger.load().events[: len(original.events)] == original.events
    if action == "exit":
        # A reported partial sale is a fact even with no canonical holding quotes.
        sell = build_sell_execution_draft(
            ledger,
            occurred_at=NOW,
            ticker="2331",
            quantity=100,
            price_yen=Decimal(1000),
            decision_reference=draft.position_review_id,
        )
        apply_draft(ledger, sell, human_confirmed=True)
        state = replay_events_through(ledger.load().events, NOW)
        assert sum(lot.quantity for lot in state.lots["2331"]) == 100


def test_relevant_quote_change_requires_reconfirmation_without_overwriting_remaining(holding_case):
    db, market, _ = holding_case
    service = PositionReviewService(db, sqlite_path=market, clock=lambda: NOW)
    draft = service.build(thesis_id="current", position_id="2331")
    draft = draft.model_copy(
        update={
            "remaining_reward": RemainingReward(status="sufficient", reason="独立した経済判断"),
            "action": "hold",
        }
    )
    with sqlite3.connect(market) as connection:
        connection.execute("UPDATE jquants_daily_bars SET close=1001 WHERE traded_at='2026-09-04'")
    with pytest.raises(ResearchConflictError, match="quote changed"):
        service.publish(draft, confirmed=True)
    assert draft.remaining_reward.reason == "独立した経済判断"
    assert list_position_review_publications(db) == []


def test_verified_break_without_price_has_no_invented_sell_quantity(holding_case):
    db, market, _ = holding_case
    thesis, review = pair_payload(ticker="2331")
    thesis["investment_case"].update(
        status="broken", status_reason="主要契約喪失を一次開示で確認、投資理由が不成立"
    )
    thesis["judgment"]["disposition"] = "reject"
    thesis["input_snapshot"]["facts"] = []
    thesis["valuation"] = dict(
        status="unresolved",
        market_price_fact_id=None,
        horizon_months=None,
        required_annual_return_pct=None,
        base=None,
        downside=None,
        unresolved_reason="回収価値未確定",
    )
    review.update(
        review_id="break-review",
        recalculated_projections=[],
        reviewed_thesis_sha256=thesis_core_hash(ThesisDocument.model_validate(thesis)),
    )
    ThesisStoreService(db, clock=lambda: NOW).publish_reviewed_thesis(
        "current-break", thesis, review, supersedes_id="current"
    )
    market.unlink()
    service = PositionReviewService(db, sqlite_path=market, clock=lambda: NOW)
    draft = service.build(thesis_id="current-break", position_id="2331")
    assert draft.quote is None
    assert draft.remaining_reward is None
    assert draft.action == "exit"
    assert service.check(draft).sell_quantity is None
    assert service.publish(draft, confirmed=True) == draft


def test_split_or_missing_target_bar_cannot_produce_quantity_bearing_exit(holding_case):
    db, market, _ = holding_case
    with sqlite3.connect(market) as connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET adjustment_factor=0.5 WHERE traded_at='2026-07-01'"
        )
    service = PositionReviewService(db, sqlite_path=market, clock=lambda: NOW)
    draft = service.build(thesis_id="current", position_id="2331")
    assert not draft.holding.quantity_basis_confirmed
    assert service.check(draft).sell_quantity is None
    assert service.publish(draft, confirmed=True).action is None


def test_today_quote_is_separate_from_original_quote_and_does_not_republish_thesis(holding_case):
    db, market, _ = holding_case
    thesis, review = pair_payload(ticker="2331", quote_as_of="2026-09-07")
    # The quote precedes the author's actual evening judgment.
    thesis["input_snapshot"]["sources"][0]["retrieved_at"] = "2026-09-07T15:45:00+09:00"
    review.update(
        review_id="today-review",
        reviewed_thesis_sha256=thesis_core_hash(ThesisDocument.model_validate(thesis)),
    )
    ThesisStoreService(db, clock=lambda: NOW).publish_reviewed_thesis(
        "today", thesis, review, supersedes_id="current"
    )
    with sqlite3.connect(market) as connection:
        connection.execute("INSERT INTO jquants_daily_bars VALUES ('2331','2026-09-07',1005,1)")
    service = PositionReviewService(db, sqlite_path=market, clock=lambda: NOW)
    draft = service.build(thesis_id="today", position_id="2331")
    assert draft.quote.observed_at.date() == NOW.date()
    assert draft.quote.price_yen == 1005
    assert draft.quote.basis_confirmed
    draft = draft.model_copy(
        update={
            "remaining_reward": RemainingReward(
                status="sufficient", reason="現在価格1005円で見返りを確認"
            ),
            "action": "hold",
        }
    )
    assert service.check(draft).action == "hold"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM thesis").fetchone()[0] == 2
