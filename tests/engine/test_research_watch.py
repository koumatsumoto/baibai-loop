"""Watchは原評価の算術と最新pairを読み、現在の売買判断・保存変更を行わない。"""

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.assessment_v1 import (
    assessment_id,
    pair_payload,
    update_review_hash,
)
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.research.assessment_store import AssessmentStoreService
from baibai_engine.research_watch import ResearchPriceWatchError, build_watch
from baibai_engine.storage.observations.schema import open_connection


@pytest.fixture
def case(tmp_path):
    db, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(db, ledger.model_copy(update={"market_prices": ()}))
    document, review = pair_payload(ticker="2331", as_of="2026-09-07", quote_as_of="2026-09-04")
    document["valuation"]["required_annual_return_pct"] = "12"
    document["valuation"]["base"].update(
        terminal_value_per_share_yen="1204",
        cash_distribution_per_share_yen="30",
    )
    review["recalculated_projections"][0].update(
        terminal_value_per_share_yen="1204",
        cash_distribution_per_share_yen="30",
    )
    update_review_hash(document, review)
    AssessmentStoreService(
        db, clock=lambda: datetime.fromisoformat("2026-09-07T18:00:00+09:00")
    ).publish_reviewed_assessment(assessment_id(), document, review)
    with open_connection(market) as connection:
        for offset in range(5):
            day = date(2026, 9, 4) + timedelta(days=offset)
            connection.execute(
                "INSERT INTO jquants_market_calendar VALUES (?,?)",
                (day.isoformat(), int(day.weekday() < 5)),
            )
            if day.weekday() < 5:
                connection.execute(
                    "INSERT INTO jquants_daily_bars(ticker,traded_at,close,adjustment_factor) VALUES ('2331',?,1050,1)",
                    (day.isoformat(),),
                )
        connection.execute(
            "INSERT INTO source_coverage(source,coverage_key,coverage_start,coverage_end,fetched_at_utc,record_count,status) VALUES ('jquants_market_calendar','test','2026-09-04','2026-09-08','2026-09-08T10:00:00Z',5,'ok')"
        )
    return db, market


def test_watch_no_write_preserves_original_horizon_and_price_basis(case):
    db, market = case
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (db, market)}
    payload = build_watch(app_db_path=db, sqlite_path=market, asof=date(2026, 9, 8))
    row = payload["rows"][0]
    assert row["status"] == "resolved"
    assert row["assessment_id"] == assessment_id()
    assert row["assessment_stance"] == "supported"
    assert row["assessment_as_of"] == "2026-09-07"
    assert row["original_quote_as_of"] == "2026-09-04"
    assert row["horizon_months"] == 12
    assert row["current_decision_status"] == "not_evaluated"
    assert 1101 < row["pmax_raw_yen"] < 1102
    assert "annualized_return_pct" not in row
    assert payload["coverage"]["assessment_tickers"] == ["2331"]
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (db, market)}


@pytest.mark.parametrize("valuation_unresolved", [False, True])
def test_latest_unsupported_or_unresolved_does_not_resurrect_supported(case, valuation_unresolved):
    db, market = case
    document, review = pair_payload(ticker="2331", as_of="2026-09-08", review_number=4)
    document["investment_case"]["status"] = "broken"
    document["conclusion"]["stance"] = "unsupported"
    if valuation_unresolved:
        document["input_snapshot"]["facts"] = []
        document["valuation"] = dict(
            status="unresolved",
            market_price_fact_id=None,
            horizon_months=None,
            required_annual_return_pct=None,
            base=None,
            downside=None,
            unresolved_reason="回収価値の確認未了",
        )
        review["recalculated_projections"] = []
    update_review_hash(document, review)
    AssessmentStoreService(
        db, clock=lambda: datetime.fromisoformat("2026-09-08T18:00:00+09:00")
    ).publish_reviewed_assessment(assessment_id(3), document, review, supersedes_id=assessment_id())
    row = build_watch(app_db_path=db, sqlite_path=market, asof=date(2026, 9, 8))["rows"][0]
    assert row["assessment_id"] == assessment_id(3)
    assert row["assessment_stance"] == "unsupported"
    assert row["assessment_as_of"] == "2026-09-08"
    if valuation_unresolved:
        assert row["pmax_raw_yen"] is None
        assert row["unresolved_reason"] == "valuation_unresolved"


def test_latest_corrupt_pair_remains_explicitly_unresolved_without_fallback(case):
    db, market = case
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO stock_assessment(assessment_id,ticker,as_of,stance,published_at,payload,core_sha256) VALUES (?,'2331','2026-09-08','uncertain','2026-09-08T18:00:00+09:00',?,'corrupt-hash')",
            (assessment_id(3), json.dumps({"kind": "stock_assessment", "schema_version": 1})),
        )
    rows = build_watch(app_db_path=db, sqlite_path=market, asof=date(2026, 9, 8))["rows"]
    assert len(rows) == 1
    assert rows[0]["assessment_id"] == assessment_id(3)
    assert rows[0]["unresolved_reason"] == "requires_reassessment"


def test_future_latest_assessment_is_not_used_or_hidden_by_an_old_revision(case):
    db, market = case
    with pytest.raises(ResearchPriceWatchError, match="future assessment"):
        build_watch(app_db_path=db, sqlite_path=market, asof=date(2026, 9, 4))
