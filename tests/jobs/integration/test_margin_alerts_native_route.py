"""Margin-alert native routing preserves independent acquisition and mixed stages."""

import gzip
import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from tests.helpers.margin_alerts_native import AT, DAY, attempt, records
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.margin_alerts_native import MarginAlertsL1
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


@pytest.mark.parametrize("tamper", [False, True])
def test_mixed_stage_skips_only_exact_migrated_alerts(tmp_path, tamper):
    store = MemoryR2Store()
    owner = MarginAlertsL1(store, tmp_path / "native")
    owner.initialize()
    received = replace(attempt("alert"), attempted_at=None)
    owner.commit([received])
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    with closing(open_connection(source)) as connection, connection:
        receive_page(connection, received.pages[0], request_id="alert", page_index=0)
        short = JQuantsPage(
            "short_sale_reports", {"disc_date": DAY.isoformat()}, AT, b'{"data":[]}'
        )
        receive_page(connection, short, request_id="short", page_index=0)
        if tamper:
            changed = attempt("other", rows=records(long=123))
            connection.execute("DELETE FROM jquants_source_pages WHERE request_id='alert'")
            receive_page(connection, changed.pages[0], request_id="alert", page_index=0)
    fingerprints = {
        key: value["fingerprint"] for key, value in owner.fixed().value()["receipts"].items()
    }
    if tamper:
        with pytest.raises(ValueError, match="differs from the native cutover"):
            import_handoff(
                source,
                target,
                ("alert", "short"),
                sources=MARKET_SOURCES,
                margin_alerts_fingerprints=fingerprints,
            )
    else:
        result = import_handoff(
            source,
            target,
            ("alert", "short"),
            sources=MARKET_SOURCES,
            margin_alerts_fingerprints=fingerprints,
        )
        assert result.complete
        assert result.report["migrated_margin_alerts_requests"] == ["alert"]
        assert result.report["replayed_requests"] == ["short"]
        with sqlite3.connect(target) as connection:
            assert connection.execute(
                "SELECT DISTINCT source FROM jquants_source_pages"
            ).fetchall() == [("short_sale_reports",)]
