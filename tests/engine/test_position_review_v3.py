"""F12-F19/F23: current holding judgment and reported facts on a legacy ledger."""

import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.research_v4 import pair_payload

from baibai_engine.operation.models import OperationPayload
from baibai_engine.operation.service import OperationService
from baibai_engine.position.drafts import apply_draft, build_event_draft, build_sell_execution_draft
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
    from tests.helpers.screening_sqlite import seed_daily_bars

    seed_daily_bars(
        market,
        [
            ("2331", (date(2026, 6, 3) + timedelta(days=i)).isoformat(), 1000.0, 1.0)
            for i in range(94)
            if (date(2026, 6, 3) + timedelta(days=i)).weekday() < 5
        ],
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
        update={"remaining_reward": RemainingReward(status=reward, reason=reason)}
    )
    # An unrelated contribution must not invalidate the author's economic judgment.
    ledger = LedgerStoreService(db)
    contribution = ContributionEvent.model_validate(
        {
            "type": "contribution",
            "event_id": "confirmed",
            "occurred_at": (NOW - timedelta(minutes=1)).isoformat(),
            "amount_yen": 10000,
        }
    )
    apply_draft(ledger, build_event_draft(ledger, contribution), human_confirmed=True)
    evaluation = service.check(draft)
    assert evaluation.action == action
    published = service.publish(draft, confirmed=True)
    assert published.remaining_reward.reason == reason
    assert published.action == action
    assert service.publish(draft, confirmed=True) == published
    wrong_action = draft.model_copy(update={"action": "exit" if action == "hold" else "hold"})
    assert service.check(wrong_action).action == action
    assert service.publish(wrong_action, confirmed=True) == published
    later = PositionReviewService(db, sqlite_path=market, clock=lambda: NOW + timedelta(days=1))
    assert later.publish(draft, confirmed=True) == published
    changed = draft.model_copy(
        update={"remaining_reward": RemainingReward(status=reward, reason="別の判断")}
    )
    with pytest.raises(ResearchConflictError, match="immutable"):
        service.publish(changed, confirmed=True)
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
    assert draft.action is None
    assert service.check(draft).sell_quantity is None
    assert service.publish(draft, confirmed=True).action == "exit"


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
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close,adjustment_factor) VALUES ('2331','2026-09-07',1250,1)"
        )
    service = PositionReviewService(db, sqlite_path=market, clock=lambda: NOW)
    draft = service.build(thesis_id="today", position_id="2331")
    assert draft.quote.observed_at.date() == NOW.date()
    assert draft.quote.price_yen == 1250
    assert draft.quote.basis_confirmed
    draft = draft.model_copy(
        update={
            "remaining_reward": RemainingReward(
                status="sufficient", reason="現在価格1250円で経済的な見返りを確認"
            ),
        }
    )
    evaluation = service.check(draft)
    assert evaluation.action == "hold"
    projection = evaluation.current_price_projection
    assert projection["price_yen"] == 1250
    assert projection["horizon_months"] == 12
    assert projection["base"]["cash_distribution_per_share_yen"] == 30
    assert projection["base"]["total_return_pct"] == -1.28
    assert projection["base"]["annualized_return_pct"] == -1.28
    assert projection["downside"]["total_return_pct"] == -52
    assert projection["downside"]["annualized_return_pct"] == -52
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM thesis").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("column", "value", "day", "expected"),
    [
        ("close", None, "2026-07-01", "exit"),
        ("close", None, "2026-09-04", None),
        ("adjustment_factor", None, "2026-07-01", None),
        ("adjustment_factor", 0.5, "2026-07-01", None),
    ],
)
def test_current_price_and_historical_unit_evidence_are_independent(
    holding_case, column, value, day, expected
):
    db, market, _ = holding_case
    with sqlite3.connect(market) as connection:
        # The other ticker provides market-wide sessions even if subject coverage is incomplete.
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close,adjustment_factor) SELECT '9999',traded_at,500,1 FROM jquants_daily_bars"
        )
        connection.execute(
            f"UPDATE jquants_daily_bars SET {column}=? WHERE ticker='2331' AND traded_at=?",
            (value, day),
        )
    service = PositionReviewService(db, sqlite_path=market, clock=lambda: NOW)
    draft = service.build(thesis_id="current", position_id="2331")
    draft = draft.model_copy(
        update={
            "remaining_reward": RemainingReward(
                status="insufficient", reason="将来増分の見返り不足"
            ),
        }
    )
    assert (service.check(draft).current_price_projection is not None) == (expected == "exit")
    assert service.check(draft).action == expected
    assert service.publish(draft, confirmed=True).action == expected


@pytest.mark.parametrize(
    ("kind", "details", "delta"),
    [
        ("contribution", {}, 1000),
        ("income", {"ticker": "2331", "income_kind": "dividend"}, 1000),
        ("cost", {"ticker": "2331", "cost_kind": "commission"}, -1000),
        ("tax_confirmed", {"ticker": "2331", "tax_kind": "dividend"}, -1000),
        ("withdrawal", {}, -1000),
    ],
)
def test_human_event_draft_apply_needs_no_holding_price(holding_case, kind, details, delta):
    from pydantic import TypeAdapter

    from baibai_engine.position.drafts import HumanEvent
    from baibai_engine.position.ledger import PortfolioLedgerError

    db, _, _ = holding_case
    service = LedgerStoreService(db)
    before = replay_events_through(service.load().events, NOW)
    event = TypeAdapter(HumanEvent).validate_python(
        dict(
            type=kind,
            event_id="human-event",
            occurred_at=NOW,
            amount_yen=1000,
            **details,
        )
    )
    draft = build_event_draft(service, event)
    apply_draft(service, draft, human_confirmed=True)
    assert service.load().market_prices == ()
    after = replay_events_through(service.load().events, NOW)
    assert after.available_cash_yen == before.available_cash_yen + delta
    if delta < 0:
        with pytest.raises(PortfolioLedgerError):
            build_event_draft(
                service,
                event.model_copy(
                    update={"event_id": "overdraw", "amount_yen": after.available_cash_yen + 1}
                ),
            )
    with pytest.raises(PortfolioLedgerError, match="exceeds"):
        build_sell_execution_draft(
            service, occurred_at=NOW, ticker="2331", quantity=300, price_yen=Decimal(1000)
        )


@pytest.mark.parametrize(
    ("reward", "action"), [("sufficient", "hold"), ("insufficient", "exit"), ("uncertain", None)]
)
def test_legacy_holding_first_review_follows_public_workspace_commands(
    holding_case, app_method_root, tmp_path, monkeypatch, capsys, reward, action
):
    import yaml

    from baibai_engine.position import cli as position_cli
    from baibai_engine.research.workspace_cli import main as research_main

    _, market, _ = holding_case
    db = app_method_root / "stores/application/baibai.sqlite"
    with sqlite3.connect(db) as connection:
        original = connection.execute("SELECT thesis_id,payload,core_sha256 FROM thesis").fetchone()
        original_events = connection.execute(
            "SELECT payload FROM ledger_event ORDER BY append_seq"
        ).fetchall()
        connection.execute("DELETE FROM ledger_market_price")
    workspace = tmp_path / "holding-workspace"
    common = ["--db", str(db), "--ticker", "2331", "--workspace", str(workspace)]
    monkeypatch.setattr(position_cli, "MARKET_DB_PATH", market)
    assert position_cli.main(["ledger", "--db", str(db)], now=NOW) == 0
    assert (
        research_main(["position-prepare", *common, "--asof", NOW.date().isoformat()], now=NOW) == 0
    )
    # Legacy is deliberately not passed to the v4 refresh reader.
    assert (
        research_main(
            [
                "thesis-scaffold",
                *common,
                "--sqlite-path",
                str(market),
                "--target-session",
                NOW.date().isoformat(),
            ],
            now=NOW,
        )
        == 0
    )
    thesis_path = workspace / "2331/thesis-draft.yaml"
    assert yaml.safe_load(thesis_path.read_text())["schema_version"] == 4
    # Author today's evidence and independent calculation, not a v3 conversion.
    thesis, review = pair_payload(ticker="2331")
    thesis_path.write_text(yaml.safe_dump(thesis))
    assert research_main(["review-scaffold", *common], now=NOW) == 0
    review_path = workspace / "2331/2026-09-07-2331-decision-review.yaml"
    assert (
        yaml.safe_load(review_path.read_text())["reviewed_thesis_sha256"]
        == review["reviewed_thesis_sha256"]
    )
    review_path.write_text(yaml.safe_dump(review))
    assert (
        research_main(
            [
                "promote",
                *common,
                "--thesis-id",
                "first-v4",
                "--supersedes-id",
                original[0],
            ],
            now=NOW,
        )
        == 0
    )
    draft_path = workspace / "position-review.yaml"
    assert (
        position_cli.main(
            [
                "position-review-build",
                "--db",
                str(db),
                "--thesis-id",
                "first-v4",
                "--position-id",
                "2331",
                "--sqlite",
                str(market),
                "--out",
                draft_path.name,
                "--root",
                str(workspace),
            ],
            now=NOW,
        )
        == 0
    )
    draft = yaml.safe_load(draft_path.read_text())
    assert draft["action"] is None
    assert "current_price_projection" not in draft
    assert "current_price_projection:" in capsys.readouterr().out
    draft["remaining_reward"] = {"status": reward, "reason": "現在の証拠から残存見返りを確認"}
    draft_path.write_text(yaml.safe_dump(draft))
    assert (
        position_cli.main(
            [
                "position-review",
                "--input",
                str(draft_path),
                "--db",
                str(db),
                "--sqlite",
                str(market),
            ],
            now=NOW,
        )
        == 0
    )
    checked = yaml.safe_load(capsys.readouterr().out)
    assert checked["action"] == action
    assert checked["current_price_projection"]["base"]["total_return_pct"] == 23.4
    assert (
        position_cli.main(
            [
                "position-review",
                "publish",
                str(draft_path),
                "--db",
                str(db),
                "--thesis-id",
                "first-v4",
                "--sqlite",
                str(market),
                "--confirmed",
                "--root",
                str(workspace),
            ],
            now=NOW,
        )
        == 0
    )
    assert yaml.safe_load(capsys.readouterr().out)["action"] == action
    published = list_position_review_publications(db)[0]["payload"]
    assert published["action"] == action
    assert "current_price_projection" not in published
    with sqlite3.connect(db) as connection:
        assert (
            connection.execute(
                "SELECT thesis_id,payload,core_sha256 FROM thesis WHERE thesis_id=?", (original[0],)
            ).fetchone()
            == original
        )
        assert (
            connection.execute("SELECT payload FROM ledger_event ORDER BY append_seq").fetchall()
            == original_events
        )
    assert OperationService(db).active() is None


def test_override_constraints_are_price_independent_and_expiry_uses_valuation_instant(holding_case):
    from baibai_engine.position.drafts import build_override_draft
    from baibai_engine.position.ledger import HumanOverride, PortfolioLedgerError
    from baibai_engine.position.valuation import current_portfolio

    db, market, ledger = holding_case
    service = LedgerStoreService(db)
    accepted = HumanOverride.model_validate(
        dict(
            override_id="accepted",
            scope="ticker",
            key="2331",
            reason="一時的な集中を確認",
            decision_reference="human-confirmation",
            approved_at=ledger.as_of,
            expires_at=ledger.as_of + timedelta(days=1),
        )
    )
    too_long = accepted.model_copy(update={"expires_at": ledger.as_of + timedelta(days=32)})
    before = service.load()
    with pytest.raises(PortfolioLedgerError, match="exceeds 31 days"):
        build_override_draft(service, too_long)
    valid = build_override_draft(service, accepted)
    # Hand-editing a draft cannot bypass the same price-independent apply boundary.
    tampered = valid.model_copy(
        update={"replacement": valid.replacement.model_copy(update={"overrides": (too_long,)})}
    )
    with pytest.raises(PortfolioLedgerError, match="exceeds 31 days"):
        apply_draft(service, tampered, human_confirmed=True)
    assert service.load() == before
    apply_draft(service, valid, human_confirmed=True)
    with pytest.raises(PortfolioLedgerError, match="multiple active overrides"):
        build_override_draft(service, accepted.model_copy(update={"override_id": "duplicate"}))
    with sqlite3.connect(market) as connection:
        connection.execute("UPDATE jquants_daily_bars SET close=10000 WHERE traded_at='2026-09-04'")
    snapshot = current_portfolio(service.load(), sqlite_path=market, now=NOW)
    warning = next(
        item for item in snapshot.warnings if item.scope == "ticker" and item.key == "2331"
    )
    assert warning.actual_pct > 10
    assert not warning.overridden
    assert snapshot.as_of == ledger.as_of


@pytest.mark.parametrize("factor", [1.0, 0.5, None])
def test_intraday_fill_must_bridge_quote_and_quantity_dates(tmp_path, factor):
    from tests.helpers.screening_sqlite import seed_daily_bars

    from baibai_engine.position.ledger import PortfolioLedgerDocument
    from baibai_engine.position.valuation import current_portfolio, holding_quote

    now = NOW.replace(hour=10)
    ledger = PortfolioLedgerDocument.model_validate(
        dict(
            schema_version=2,
            portfolio_scope="repository_only",
            as_of=now.isoformat(),
            market_prices=[],
            events=[
                dict(
                    type="opening_balance",
                    event_id="opening",
                    occurred_at="2026-09-07T08:00:00+09:00",
                    amount_yen=1000000,
                ),
                dict(
                    type="reservation",
                    event_id="reserve",
                    occurred_at="2026-09-07T09:00:00+09:00",
                    reservation_id="order",
                    order_id="order",
                    ticker="2331",
                    sector="test",
                    common_factors=[],
                    quantity=100,
                    price_guard_yen=500,
                    expires_at="2026-09-07T15:30:00+09:00",
                ),
                dict(
                    type="execution",
                    event_id="fill",
                    occurred_at="2026-09-07T09:01:00+09:00",
                    execution_id="fill",
                    reservation_id="order",
                    ticker="2331",
                    side="buy",
                    quantity=100,
                    price_yen=500,
                ),
            ],
        )
    )
    market = tmp_path / "market.sqlite"
    seed_daily_bars(market, [("2331", "2026-09-04", 1000, 1), ("2331", "2026-09-07", None, factor)])
    quote, confirmed = holding_quote(ledger, ticker="2331", sqlite_path=market, now=now)
    assert quote.price_as_of.isoformat() == "2026-09-04"
    assert confirmed is (factor == 1.0)
    snapshot = current_portfolio(ledger, sqlite_path=market, now=now)
    assert snapshot.available_cash_yen == 950000
    assert snapshot.holdings[0].quantity == 100
    assert snapshot.holdings[0].deployed_cost_yen == 50000
    assert snapshot.total_capital_yen == (1050000 if factor == 1.0 else None)


def test_current_portfolio_leaves_stale_market_quotes_unvalued(holding_case):
    from baibai_engine.position.valuation import current_portfolio

    _, market, ledger = holding_case
    snapshot = current_portfolio(ledger, sqlite_path=market, now=NOW + timedelta(days=30))
    assert snapshot.available_cash_yen == 10080500
    assert snapshot.holdings[0].quantity == 200
    assert snapshot.holdings[0].market_value_yen is None
    assert snapshot.total_capital_yen is None
