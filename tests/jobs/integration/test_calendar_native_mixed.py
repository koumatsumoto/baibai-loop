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
from baibai_jobs.integration.jquants_handoff import MARKET_SOURCES, import_handoff


def packet(path, calendar):
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
                gzip.compress(b'{"data":[]}', mtime=0),
            ),
        )


def test_partial_calendar_raw_proof_does_not_block_remaining_source_replay(tmp_path):
    source = CalendarL1(MemoryR2Store(), tmp_path / "native")
    source.initialize()
    raw = attempt("old-calendar", partial=True)
    source.commit([raw])
    fingerprints = {
        key: value["fingerprint"] for key, value in source.fixed().value()["receipts"].items()
    }
    old = tmp_path / "old.sqlite"
    packet(old, raw)
    target = market_store(tmp_path / "target.sqlite")
    result = import_handoff(
        old,
        target,
        ["old-calendar", "other"],
        sources=MARKET_SOURCES,
        calendar_fingerprints=fingerprints,
    )
    assert result.complete
    assert result.report["migrated_calendar_requests"] == ["old-calendar"]
    assert result.report["replayed_requests"] == ["other"]
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT DISTINCT source FROM jquants_source_pages").fetchall() == [
            ("margin_alerts",)
        ]


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
