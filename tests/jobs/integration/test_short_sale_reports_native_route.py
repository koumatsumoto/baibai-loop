"""Ordinary short-report routing stays independent of L2 and exact on old stages."""

import gzip
import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from tests.helpers.jquants_receipts import body
from tests.helpers.margin_alerts_native import attempt as alert_attempt
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.short_sale_reports_native import AT, DAY, attempt, records

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.margin_alerts_native import MarginAlertsL1
from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    ShortSaleReportsL1,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.jquants_handoff import MARKET_SOURCES, import_handoff


def receive_page(connection, page, *, request_id, page_index):
    connection.execute(
        "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
        (
            request_id,
            page_index,
            page.source,
            json.dumps(dict(page.query)),
            page.received_at.isoformat(),
            page.received_at.date().isoformat(),
            gzip.compress(page.body, mtime=0),
        ),
    )


@pytest.mark.parametrize("migrated_alerts", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_mixed_stage_only_skips_verified_migrated_sources(tmp_path, migrated_alerts, partial):
    store = MemoryR2Store()
    owner = ShortSaleReportsL1(store, tmp_path / "short-native")
    owner.initialize()
    short = replace(attempt("short", partial=partial), attempted_at=None, failure=None)
    owner.commit([short])
    alert_owner = MarginAlertsL1(store, tmp_path / "alert-native")
    alert_owner.initialize()
    alert = replace(alert_attempt("alert"), attempted_at=None)
    alert_owner.commit([alert])
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    with closing(open_connection(source)) as connection, connection:
        receive_page(connection, short.pages[0], request_id="short", page_index=0)
        receive_page(connection, alert.pages[0], request_id="alert", page_index=0)
    fingerprints = {
        key: receipt["fingerprint"] for key, receipt in owner.fixed().value()["receipts"].items()
    }
    alert_fingerprints = {
        key: receipt["fingerprint"]
        for key, receipt in alert_owner.fixed().value()["receipts"].items()
    }
    result = import_handoff(
        source,
        target,
        ("short", "alert"),
        sources=MARKET_SOURCES,
        short_sale_reports_fingerprints=fingerprints,
        margin_alerts_fingerprints=alert_fingerprints if migrated_alerts else None,
    )
    assert result.complete
    assert result.report["migrated_short_sale_reports_requests"] == ["short"]
    assert result.report["replayed_requests"] == ([] if migrated_alerts else ["alert"])
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT DISTINCT source FROM jquants_source_pages"
        ).fetchall() == ([] if migrated_alerts else [("margin_alerts",)])


@pytest.mark.parametrize("damage", ["absent", "body", "clock", "page_gap", "filtered"])
def test_short_stage_fingerprint_mismatch_is_not_dropped(tmp_path, damage):
    from datetime import timedelta

    store = MemoryR2Store()
    owner = ShortSaleReportsL1(store, tmp_path / "native")
    owner.initialize()
    original = replace(attempt("short"), attempted_at=None)
    owner.commit([original])
    fingerprints = {
        key: receipt["fingerprint"] for key, receipt in owner.fixed().value()["receipts"].items()
    }
    page, index = original.pages[0], 0
    if damage == "absent":
        fingerprints = {}
    elif damage == "body":
        page = JQuantsPage(page.source, page.query, page.received_at, body(records(name="changed")))
    elif damage == "clock":
        page = JQuantsPage(page.source, page.query, AT + timedelta(seconds=1), page.body)
    elif damage == "page_gap":
        index = 1
    else:
        page = JQuantsPage(page.source, {"date": DAY.isoformat()}, page.received_at, page.body)
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    with closing(open_connection(source)) as connection, connection:
        receive_page(connection, page, request_id="short", page_index=index)
    with pytest.raises(ValueError, match="legacy short_sale_reports"):
        import_handoff(
            source,
            target,
            ("short",),
            sources=MARKET_SOURCES,
            short_sale_reports_fingerprints=fingerprints,
        )
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jquants_source_pages").fetchone()[0] == 0
