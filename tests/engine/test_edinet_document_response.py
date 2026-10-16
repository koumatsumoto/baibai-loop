from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date

import pytest
from tests.helpers.http_doubles import FakeSession, json_response

from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.market.providers.edinet import EDINETProvider
from baibai_engine.storage.observations.ingest.edinet import store_edinet_documents


def _saved_response(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT response_json FROM edinet_document_lists WHERE doc_date = ?",
            ("2026-10-15",),
        ).fetchone()[0]


def test_listing_keeps_native_fields_without_invented_request_date(tmp_path):
    payload = {
        "metadata": {"resultset": {"count": 1}, "processDateTime": "2026-10-15 12:00"},
        "results": [{"seqNumber": 1, "docID": "S100TEST", "filerName": "提出者",
                     "futureField": {"zero": 0, "missing": None}}],
        "futureMetadata": [False, ""],
    }
    path = tmp_path / "market.sqlite"
    provider = EDINETProvider(
        "test-key", tmp_path / "cache", session=FakeSession(json_response(payload)),
        sqlite_path=path,
    )
    documents = provider.list_documents(date(2026, 10, 15))
    assert documents[0]["doc_date"] == "2026-10-15"
    original = json.loads(_saved_response(path))
    assert original == payload
    assert "doc_date" not in original["results"][0]
    assert "periodStart" not in original["results"][0]


def test_refresh_keeps_new_nulls_and_empty_response_not_previous_raw_values(tmp_path):
    path = tmp_path / "market.sqlite"
    payloads = [
        {"metadata": {"resultset": {"count": 1}},
         "results": [{"seqNumber": 1, "docID": "S100TEST", "docDescription": "有価証券報告書"}]},
        {"metadata": {"resultset": {"count": 1}},
         "results": [{"seqNumber": 1, "docID": "S100TEST", "docDescription": None}]},
        {"metadata": {"resultset": {"count": 0}}, "results": []},
    ]
    provider = EDINETProvider(
        "test-key", tmp_path / "cache",
        session=FakeSession(*(json_response(value) for value in payloads)), sqlite_path=path,
    )
    for index, payload in enumerate(payloads):
        provider.list_documents(date(2026, 10, 15), force_refresh=True)
        assert json.loads(_saved_response(path)) == payload
        if index == 1:
            with closing(sqlite3.connect(path)) as connection:
                assert connection.execute(
                    "SELECT doc_description FROM edinet_documents"
                ).fetchone() == ("有価証券報告書",)


def test_invalid_refresh_does_not_replace_last_valid_response(tmp_path):
    path = tmp_path / "market.sqlite"
    valid = {"metadata": {"resultset": {"count": 0}}, "results": []}
    invalid = {"metadata": {"resultset": {"count": 1}}, "results": []}
    provider = EDINETProvider(
        "test-key", tmp_path / "cache",
        session=FakeSession(json_response(valid), json_response(invalid)), sqlite_path=path,
    )
    provider.list_documents(date(2026, 10, 15))
    with pytest.raises(EDINETProviderError, match="count mismatch"):
        provider.list_documents(date(2026, 10, 15), force_refresh=True)
    assert json.loads(_saved_response(path)) == valid


def test_normalized_import_does_not_claim_to_recover_an_original_response(tmp_path):
    path = tmp_path / "market.sqlite"
    store_edinet_documents(path, date(2026, 10, 15), [])
    assert _saved_response(path) is None
