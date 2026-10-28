"""Provider list reads the retained source before trusting any derived row."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.edinet_documents import store_edinet_documents
from tests.helpers.http_doubles import FakeSession, json_response

from baibai_engine.data_collection.providers.edinet.receipts import (
    read_list_responses,
    retain_list_response,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import (
    rebuild_edinet_document_day,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    read_edinet_documents,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh

DAY = date(2026, 10, 15)
STAMP = datetime(2026, 10, 15, 12, tzinfo=UTC)


def _retain(path, body: bytes, *, seconds: int) -> None:
    with closing(open_connection(path)) as connection:
        retain_list_response(
            connection,
            doc_date=DAY,
            received_at=STAMP + timedelta(seconds=seconds),
            body=body,
        )


def _coverage(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT status, record_count FROM source_coverage "
            "WHERE source = 'edinet_documents' AND coverage_key = ?",
            (DAY.isoformat(),),
        ).fetchone()


def test_offline_rebuild_requires_existing_store(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(EDINETProviderError, match="database is missing"):
        rebuild_edinet_document_day(path, DAY)
    assert not path.exists()


@pytest.mark.parametrize("derived_state", ["absent", "stale", "corrupt"])
def test_offline_rebuild_uses_raw_receipt_over_derived_state(tmp_path, derived_state):
    path = tmp_path / "market.sqlite"
    if derived_state != "absent":
        store_edinet_documents(path, DAY, [{"seqNumber": 1, "docID": "S100OLD"}])
    if derived_state == "corrupt":
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "UPDATE edinet_document_lists SET response_json = ? WHERE doc_date = ?",
                ("{invalid", DAY.isoformat()),
            )
    _retain(
        path,
        b'{"metadata":{"resultset":{"count":1}},"results":[{"seqNumber":1,"docID":"S100NEW","secCode":"72030"}]}',
        seconds=1,
    )
    projection = rebuild_edinet_document_day(path, DAY)
    documents = list(projection.documents)
    assert [item["docID"] for item in documents] == ["S100NEW"]
    assert [item["docID"] for item in read_edinet_documents(path, DAY)] == ["S100NEW"]
    assert _coverage(path) == ("ok", 1)


def test_pending_malformed_receipt_blocks_old_success_until_valid_acquisition(tmp_path):
    path = tmp_path / "market.sqlite"
    valid = b'{"metadata":{"resultset":{"count":1}},"results":[{"seqNumber":1,"docID":"S100GOOD"}]}'
    _retain(path, valid, seconds=1)
    assert [item["docID"] for item in rebuild_edinet_document_day(path, DAY).documents] == [
        "S100GOOD"
    ]
    _retain(path, b"{invalid JSON", seconds=2)
    assert rebuild_edinet_document_day(path, DAY).error is not None
    assert _coverage(path) == ("failed", 1)
    assert read_edinet_documents(path, DAY) is None

    acquired = EDINETRefresh(
        "key",
        sqlite_path=path,
        session=FakeSession(
            json_response(
                {
                    "metadata": {"resultset": {"count": 1}},
                    "results": [{"seqNumber": 1, "docID": "S100GOOD"}],
                }
            )
        ),
    )
    assert [item["docID"] for item in acquired.list_documents(DAY)] == ["S100GOOD"]
    assert _coverage(path) == ("ok", 1)
    with closing(open_connection(path)) as connection:
        assert len(read_list_responses(connection, doc_date=DAY)) == 3
