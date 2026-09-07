"""F08/F14/F19: watch keeps original estimates and is strictly read-only."""

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.research_v4 import pair_payload

from baibai_engine.market.sqlite import open_connection
from baibai_engine.research.thesis_store import ThesisStoreService
from baibai_engine.research_watch import build_watch


def test_watch_no_write_and_latest_legacy_does_not_fall_back(tmp_path):
    db, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(db, ledger.model_copy(update={"market_prices": ()}))
    thesis, review = pair_payload(ticker="2331")
    now = datetime.fromisoformat("2026-09-07T18:00:00+09:00")
    ThesisStoreService(db, clock=lambda: now).publish_reviewed_thesis("v4", thesis, review)
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
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (db, market)}
    row = build_watch(app_db_path=db, sqlite_path=market, asof=date(2026, 9, 8))["rows"][0]
    assert row["status"] == "resolved"
    assert row["thesis_as_of"] == "2026-09-07"
    assert row["original_quote_as_of"] == "2026-09-04"
    assert row["horizon_months"] == 12
    assert row["current_decision_status"] == "not_evaluated"
    assert 1101 < row["pmax_raw_yen"] < 1102
    assert "annualized_return_pct" not in row
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (db, market)}
    # Preserve an old-format row as raw history. It cannot resurrect the earlier candidate.
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO thesis(thesis_id,ticker,as_of,recommendation,published_at,payload,core_sha256) VALUES ('legacy-latest','2331','2026-09-08','buy','2026-09-08T18:00:00+09:00',?,'legacy-hash')",
            (json.dumps({"schema_version": 3, "historical": True}),),
        )
    rows = build_watch(app_db_path=db, sqlite_path=market, asof=date(2026, 9, 8))["rows"]
    assert len(rows) == 1
    assert rows[0]["thesis_id"] == "legacy-latest"
    assert rows[0]["unresolved_reason"] == "requires_reassessment"
