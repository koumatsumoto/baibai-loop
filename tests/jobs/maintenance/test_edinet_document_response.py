from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

from tests.helpers.edinet_documents import store_edinet_documents

from baibai_engine.data_collection.providers.edinet.receipts import (
    read_list_responses,
    retain_list_response,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
    read_edinet_documents,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import (
    rebuild_edinet_document_day,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def _saved_response(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT response_json FROM edinet_document_lists WHERE doc_date = ?",
            ("2026-10-15",),
        ).fetchone()[0]


def test_rebuild_keeps_native_fields_without_invented_request_date(tmp_path):
    payload = {
        "metadata": {"resultset": {"count": 1}, "processDateTime": "2026-10-15 12:00"},
        "results": [
            {
                "seqNumber": 1,
                "docID": "S100TEST",
                "filerName": "提出者",
                "futureField": {"zero": 0, "missing": None},
            }
        ],
        "futureMetadata": [False, ""],
    }
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as connection:
        retain_list_response(
            connection,
            doc_date=date(2026, 10, 15),
            received_at=datetime(2026, 10, 15, 3, tzinfo=UTC),
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    projection = rebuild_edinet_document_day(path, date(2026, 10, 15))
    assert projection.error is None
    documents = projection.documents
    assert documents[0]["doc_date"] == "2026-10-15"
    original = json.loads(_saved_response(path))
    assert original == payload
    assert "doc_date" not in original["results"][0]
    assert "periodStart" not in original["results"][0]


def test_rebuild_keeps_new_nulls_and_empty_response_not_previous_raw_values(tmp_path):
    path = tmp_path / "market.sqlite"
    payloads = [
        {
            "metadata": {"resultset": {"count": 1}},
            "results": [{"seqNumber": 1, "docID": "S100TEST", "docDescription": "有価証券報告書"}],
        },
        {
            "metadata": {"resultset": {"count": 1}},
            "results": [{"seqNumber": 1, "docID": "S100TEST", "docDescription": None}],
        },
        {"metadata": {"resultset": {"count": 0}}, "results": []},
    ]
    for index, payload in enumerate(payloads):
        with closing(open_connection(path)) as connection:
            retain_list_response(
                connection,
                doc_date=date(2026, 10, 15),
                received_at=datetime(2026, 10, 15, 3, tzinfo=UTC) + timedelta(seconds=index),
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
        projection = rebuild_edinet_document_day(path, date(2026, 10, 15))
        assert projection.error is None
        assert json.loads(_saved_response(path)) == payload
        if index == 1:
            with closing(sqlite3.connect(path)) as connection:
                assert connection.execute(
                    "SELECT doc_description FROM edinet_documents"
                ).fetchone() == (None,)


def test_invalid_receipt_retains_raw_and_blocks_stale_success(tmp_path):
    path = tmp_path / "market.sqlite"
    valid = {"metadata": {"resultset": {"count": 0}}, "results": []}
    invalid = {"metadata": {"resultset": {"count": 1}}, "results": []}
    for index, payload in enumerate((valid, invalid)):
        with closing(open_connection(path)) as connection:
            retain_list_response(
                connection,
                doc_date=date(2026, 10, 15),
                received_at=datetime(2026, 10, 15, 3, tzinfo=UTC) + timedelta(seconds=index),
                body=json.dumps(payload).encode("utf-8"),
            )
        projection = rebuild_edinet_document_day(path, date(2026, 10, 15))
        if index == 0:
            assert projection.error is None
        else:
            assert projection.error is not None
            assert "does not match" in projection.error
    assert json.loads(_saved_response(path)) == valid
    with closing(open_connection(path)) as connection:
        assert len(read_list_responses(connection, doc_date=date(2026, 10, 15))) == 2
        assert connection.execute(
            "SELECT status FROM source_coverage WHERE source = 'edinet_documents'"
        ).fetchone() == ("failed",)
    assert read_edinet_documents(path, date(2026, 10, 15)) is None


def test_normalized_import_does_not_claim_to_recover_an_original_response(tmp_path):
    path = tmp_path / "market.sqlite"
    store_edinet_documents(path, date(2026, 10, 15), [])
    assert _saved_response(path) is None
