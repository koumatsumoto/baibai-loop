"""Old mixed stages cannot replay calendar over the independent source head."""

import gzip
import hashlib
import json
import sqlite3

import pytest
from tests.engine.data_collection.providers.jquants.test_calendar_native import AT, DAY, attempt
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.calendar_native import CalendarL1
from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.margin_alerts_native import (
    MarginAlertsAttempt,
    MarginAlertsL1,
)
from baibai_jobs.integration.jquants_handoff import MARKET_SOURCES, import_handoff


def packet(path, calendar):
    other = JQuantsPage("margin_alerts", {"date": DAY.isoformat()}, AT, b'{"data":[]}')
    with sqlite3.connect(path) as conn:
        conn.executescript(JQUANTS_SOURCE_PAGES_DDL)
        page = calendar.pages[0]
        conn.execute(
            "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
            (
                calendar.request_id,
                0,
                "market_calendar",
                json.dumps(dict(page.query)),
                AT.isoformat(),
                DAY.isoformat(),
                gzip.compress(page.body, mtime=0),
            ),
        )
        conn.execute(
            "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
            (
                "other",
                0,
                "margin_alerts",
                json.dumps({"date": DAY.isoformat()}),
                AT.isoformat(),
                DAY.isoformat(),
                gzip.compress(other.body, mtime=0),
            ),
        )

    return other


def test_partial_calendar_and_other_native_receipts_are_verified_without_reprojection(tmp_path):
    store = MemoryR2Store()
    source = CalendarL1(store, tmp_path / "native")
    source.initialize()
    raw = attempt("old-calendar", partial=True)
    source.commit([raw])
    fingerprints = {
        key: value["fingerprint"] for key, value in source.fixed().value()["receipts"].items()
    }
    old = tmp_path / "old.sqlite"
    other = packet(old, raw)
    alerts = MarginAlertsL1(store, tmp_path / "alerts")
    alerts.initialize()
    alerts.commit([MarginAlertsAttempt("other", DAY, None, (other,))])
    target = market_store(tmp_path / "target.sqlite")
    result = import_handoff(
        old,
        target,
        ["old-calendar", "other"],
        sources=MARKET_SOURCES,
        calendar_fingerprints=fingerprints,
        margin_alerts_fingerprints={
            key: row["fingerprint"] for key, row in alerts.fixed().value()["receipts"].items()
        },
    )
    assert result.complete
    assert result.report["migrated_calendar_requests"] == ["old-calendar"]
    assert result.report["migrated_margin_alerts_requests"] == ["other"]
    assert result.report["replayed_requests"] == []
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT DISTINCT source FROM jquants_source_pages").fetchall() == []


@pytest.mark.parametrize("proof", [{}, {"old-calendar": "f" * 64}])
def test_missing_or_different_raw_proof_stops_before_any_legacy_import(tmp_path, proof):
    old = tmp_path / "old.sqlite"
    packet(old, attempt("old-calendar"))
    target = market_store(tmp_path / "target.sqlite")
    before = hashlib.sha256(target.read_bytes()).digest()
    with pytest.raises(ValueError, match="finite cutover"):
        import_handoff(
            old,
            target,
            ["old-calendar", "other"],
            sources=MARKET_SOURCES,
            calendar_fingerprints=proof,
        )
    assert hashlib.sha256(target.read_bytes()).digest() == before
