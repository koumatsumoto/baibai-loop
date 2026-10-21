"""The EDINET reader consumes retained inputs without invoking acquisition."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from unittest.mock import patch

import pytest
from tests.helpers.edinet_documents import store_edinet_documents

from baibai_engine.collection.edinet.archives import retain_source_archive
from baibai_engine.market.edinet_metrics.store import read_edinet_documents, read_edinet_metrics
from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.market.edinet_reader import EDINETReader
from baibai_engine.storage.observations.ingest.edinet import (
    store_edinet_metrics,
)
from baibai_engine.storage.observations.schema import open_connection


def test_missing_reader_inputs_never_create_a_store_or_connect(tmp_path):
    path = tmp_path / "absent" / "market.sqlite"
    local_zip = tmp_path / "edinet" / "csv_zips" / "S100TEST.zip"
    local_zip.parent.mkdir(parents=True)
    local_zip.write_bytes(b"local download cache is not a retained archive")
    reader = EDINETReader(path)
    with patch("requests.Session", side_effect=AssertionError("reader attempted HTTP")):
        assert reader.load_metric_records(date(2026, 4, 24)) == {}
        with pytest.raises(EDINETProviderError, match="document list is unavailable"):
            reader.list_documents(date(2026, 4, 24))
        with pytest.raises(EDINETProviderError, match="archive is unavailable"):
            reader.read_csv_zip("S100TEST")
        with pytest.raises(EDINETProviderError, match="archive is unavailable"):
            reader.read_xbrl_zip("S100TEST")
    assert not path.exists()
    assert not path.parent.exists()
    assert local_zip.read_bytes() == b"local download cache is not a retained archive"


def test_reader_preserves_empty_listing_and_cached_metric_rows(tmp_path):
    path = tmp_path / "market.sqlite"
    day = date(2026, 4, 24)
    store_edinet_documents(path, day, [])
    store_edinet_metrics(
        path,
        day,
        [{"ticker": "7203", "cash": 100.0, "debt": 10.0, "extractor_revision": "a" * 64}],
    )
    reader = EDINETReader(path)
    with patch("requests.Session", side_effect=AssertionError("reader attempted HTTP")):
        assert reader.list_documents(day) == []
        assert reader.load_metric_records(day) == read_edinet_metrics(path, day)
        assert reader.load_metric_records(day)["7203"].cash == 100.0
        assert reader.load_metric_records(date(2026, 4, 23)) == {}
        with pytest.raises(EDINETProviderError, match="document list is unavailable"):
            reader.list_documents(date(2026, 4, 23))


def test_reader_returns_saved_documents_and_exact_archive_bytes(tmp_path):
    path = tmp_path / "market.sqlite"
    day = date(2026, 4, 24)
    store_edinet_documents(
        path,
        day,
        [{"seqNumber": 1, "docID": "S100TEST", "secCode": "72030", "docTypeCode": "120"}],
    )
    csv_bytes = b"PK\x03\x04csv\x00\xff"
    xbrl_bytes = b"PK\x03\x04xbrl\xff\x00"
    with closing(open_connection(path)) as connection:
        retain_source_archive(connection, "S100TEST", 5, csv_bytes, stored_on=day)
        retain_source_archive(connection, "S100TEST", 1, xbrl_bytes, stored_on=day)
    reader = EDINETReader(path)
    with patch("requests.Session", side_effect=AssertionError("reader attempted HTTP")):
        assert reader.list_documents(day) == read_edinet_documents(path, day)
        assert reader.read_csv_zip("S100TEST") == csv_bytes
        assert reader.read_xbrl_zip("S100TEST") == xbrl_bytes
