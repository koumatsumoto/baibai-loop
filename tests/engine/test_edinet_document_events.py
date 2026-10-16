"""Stored event folding uses native supplied keys, not SQL null placeholders."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date

import pytest

from baibai_engine.market import edinet_document_store
from baibai_engine.market.edinet_facts.store import document_inventory
from baibai_engine.market.edinet_metrics.store import read_edinet_documents
from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.market.providers.edinet import _canonicalize_document_events
from baibai_engine.storage.observations.ingest.edinet import store_edinet_documents

ORIGIN_DAY = date(2026, 8, 1)
EDIT_DAY = date(2026, 8, 2)


def _origin():
    return {
        "seqNumber": 1,
        "docID": "S100CORR",
        "secCode": "18730",
        "docTypeCode": "130",
        "csvFlag": "1",
        "xbrlFlag": "1",
        "legalStatus": "1",
        "disclosureStatus": "0",
        "withdrawalStatus": "0",
        "docInfoEditStatus": "0",
        "parentDocID": "S100ROOT",
        "submitDateTime": "2026-08-01 10:00",
        "docDescription": "訂正有価証券報告書",
        "periodStart": "2025-04-01",
        "periodEnd": "2026-03-31",
    }


def _save_native(path, day, records):
    store_edinet_documents(
        path,
        day,
        records,
        response_json=json.dumps(
            {
                "metadata": {"resultset": {"count": len(records)}},
                "results": records,
            }
        ),
    )


@pytest.mark.parametrize("reader", ["raw", "cached", "inventory"])
@pytest.mark.parametrize("metadata", [False, True])
@pytest.mark.parametrize("explicit_null", [False, True])
def test_native_event_omission_and_null_have_the_same_meaning_after_storage(
    tmp_path, reader, metadata, explicit_null
):
    origin = _origin()
    edit = {
        "seqNumber": 2,
        "docID": origin["docID"],
        "docInfoEditStatus": "1",
        "opeDateTime": "2026-08-02 10:00",
    }
    if metadata:
        edit.update(
            {
                key: origin[key]
                for key in ("docTypeCode", "submitDateTime", "csvFlag", "xbrlFlag")
            }
        )
    if explicit_null:
        edit.update(parentDocID=None, periodEnd=None)
    path = tmp_path / "market.sqlite"
    if reader == "raw":
        documents = [
            {**origin, "doc_date": ORIGIN_DAY.isoformat()},
            {**edit, "doc_date": EDIT_DAY.isoformat()},
        ]
    else:
        _save_native(path, ORIGIN_DAY, [origin])
        _save_native(path, EDIT_DAY, [edit])
        if reader == "cached":
            originals = read_edinet_documents(path, ORIGIN_DAY)
            edits = read_edinet_documents(path, EDIT_DAY)
            assert originals is not None and edits is not None
            documents = [*originals, *edits]
        else:
            documents = document_inventory(path, EDIT_DAY)
        saved_edit = documents[-1]
        assert ("parentDocID" in saved_edit) is explicit_null
        assert ("periodEnd" in saved_edit) is explicit_null
    folded, quarantined = _canonicalize_document_events(documents, origin_start=None)
    assert not quarantined
    assert len(folded) == 1
    assert folded[0]["parentDocID"] == (None if explicit_null else "S100ROOT")
    assert folded[0]["periodEnd"] == (None if explicit_null else "2026-03-31")
    assert folded[0]["docTypeCode"] == "130"


def test_normalized_only_nulls_do_not_claim_original_key_presence(tmp_path):
    path = tmp_path / "market.sqlite"
    origin = _origin()
    store_edinet_documents(path, ORIGIN_DAY, [origin])
    store_edinet_documents(
        path,
        EDIT_DAY,
        [{
            "seqNumber": 2,
            "docID": origin["docID"],
            "docInfoEditStatus": "1",
            "parentDocID": None,
        }],
    )
    # Without the native response, this NULL cannot establish an explicit clearing.
    documents = document_inventory(path, EDIT_DAY)
    assert "parentDocID" not in documents[-1]
    folded, quarantined = _canonicalize_document_events(documents, origin_start=None)
    assert not quarantined
    assert folded[0]["parentDocID"] == "S100ROOT"
    # Origins keep the existing projected return shape, including unknown columns.
    assert documents[0]["edinetCode"] is None


@pytest.mark.parametrize("existing_transaction", [False, True])
@pytest.mark.parametrize("invalid_source", [False, True])
def test_stored_reader_owns_only_its_new_read_transaction(
    tmp_path, existing_transaction, invalid_source
):
    path = tmp_path / "market.sqlite"
    origin = _origin()
    native = {**origin, "docID": "S100OTHER"} if invalid_source else origin
    store_edinet_documents(
        path, ORIGIN_DAY, [origin], response_json=json.dumps({"results": [native]})
    )
    with closing(sqlite3.connect(path)) as connection:
        if existing_transaction:
            connection.execute("BEGIN")
        if invalid_source:
            with pytest.raises(EDINETProviderError, match="no matching native row"):
                edinet_document_store.read_stored_documents(connection, through=ORIGIN_DAY)
        else:
            assert edinet_document_store.read_stored_documents(connection, through=ORIGIN_DAY)
        assert connection.in_transaction is existing_transaction


def test_native_same_document_positions_stay_distinct_and_parse_once(tmp_path, monkeypatch):
    path = tmp_path / "market.sqlite"
    records = [
        {
            "seqNumber": 2,
            "docID": "S100CORR",
            "docInfoEditStatus": "1",
            "docDescription": "second",
        },
        {
            "seqNumber": 1,
            "docID": "S100CORR",
            "docInfoEditStatus": "1",
            "docDescription": "first",
        },
    ]
    _save_native(path, EDIT_DAY, records)
    original_loads = json.loads
    reads = []

    def loads(value):
        reads.append(value)
        return original_loads(value)

    monkeypatch.setattr(edinet_document_store.json, "loads", loads)
    rows = read_edinet_documents(path, EDIT_DAY)
    assert rows is not None
    assert [(row["seqNumber"], row["docDescription"]) for row in rows] == [
        (1, "first"),
        (2, "second"),
    ]
    assert len(reads) == 1


def test_native_record_must_match_the_stored_date_position_and_document(tmp_path):
    path = tmp_path / "market.sqlite"
    origin = _origin()
    unrelated = {**origin, "docID": "S100OTHER"}
    store_edinet_documents(
        path,
        ORIGIN_DAY,
        [origin],
        response_json=json.dumps({
            "metadata": {"resultset": {"count": 1}}, "results": [unrelated]
        }),
    )
    with pytest.raises(EDINETProviderError, match="no matching native row"):
        read_edinet_documents(path, ORIGIN_DAY)


def test_native_origin_keeps_identity_observed_before_inspection_expiry(tmp_path):
    path = tmp_path / "market.sqlite"
    origin = {**_origin(), "docTypeCode": "120", "parentDocID": None}
    _save_native(path, ORIGIN_DAY, [origin])
    expired = {
        **origin,
        "secCode": None,
        "docTypeCode": None,
        "submitDateTime": None,
        "docDescription": None,
        "legalStatus": "0",
        "csvFlag": "0",
        "xbrlFlag": "0",
    }
    _save_native(path, ORIGIN_DAY, [expired])
    rows = read_edinet_documents(path, ORIGIN_DAY)
    assert rows is not None
    assert rows[0]["secCode"] == origin["secCode"]
    assert rows[0]["docTypeCode"] == "120"
    assert rows[0]["submitDateTime"] == origin["submitDateTime"]
    assert rows[0]["docDescription"] == origin["docDescription"]
    assert rows[0]["legalStatus"] == "0"
    assert rows[0]["xbrlFlag"] == "0"
