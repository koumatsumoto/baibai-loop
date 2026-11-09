"""明示CSV取得はnative原本を確定してからSQLite消費copyを作る。"""

from contextlib import closing
from unittest.mock import Mock

import pytest
import requests
from tests.helpers.edinet_csv_zip import _edinet_csv_zip
from tests.helpers.http_doubles import ExplodingSession, FakeSession, response
from tests.jobs.acquisition.test_edinet_archive_native_acquisition import setup

from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EDINETProviderError,
    EdinetRateLimitError,
)
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh


@pytest.fixture
def native_owner(tmp_path, monkeypatch):
    owner, _, _, _ = setup(tmp_path, [])
    owner.store.close = Mock()
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_refresh.edinet.Boto3R2Store", lambda **_: owner.store
    )
    monkeypatch.setattr("baibai_engine.data_collection.providers.edinet.http.time.sleep", Mock())
    return owner


def test_download_csv_zip_rejects_invalid_doc_id_before_acquisition(tmp_path):
    with (
        closing(
            EDINETRefresh(
                "key",
                sqlite_path=tmp_path / "market.sqlite",
                session=ExplodingSession("request should not be attempted"),
            )
        ) as provider,
        pytest.raises(EDINETProviderError, match="invalid EDINET docID"),
    ):
        provider.download_csv_zip("../../etc/passwd")


@pytest.mark.parametrize("failure", ["rate_limit", "connection"])
def test_csv_retry_retains_native_original_and_reuses_it_ignoring_old_file(
    tmp_path, native_owner, failure
):
    cached = tmp_path / "edinet/csv_zips/S100TEST.zip"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"obsolete non-ZIP file")
    expected = _edinet_csv_zip([("jppfs_cor:NetSales", "CurrentYearDuration", "1000")])
    first = (
        response(b'{"StatusCode":"429","message":"Too Many Requests"}')
        if failure == "rate_limit"
        else requests.ConnectionError("temporary failure Subscription-Key=key")
    )
    session = FakeSession(first, response(expected))
    database = tmp_path / "market.sqlite"
    with closing(EDINETRefresh("key", sqlite_path=database, session=session)) as provider:
        assert provider.download_csv_zip("S100TEST") == expected
        assert native_owner.freeze().read_archive("S100TEST", 5) == expected
        assert EDINETReader(database).read_csv_zip("S100TEST") == expected
        assert provider.download_csv_zip("S100TEST") == expected
    assert len(session.calls) == 2
    assert cached.read_bytes() == b"obsolete non-ZIP file"


@pytest.mark.parametrize("failure", ["rate_limit", "connection"])
def test_exhausted_csv_retry_does_not_publish_or_create_sql_copy(tmp_path, native_owner, failure):
    answer = (
        response(b'{"StatusCode":"429","message":"Too Many Requests"}')
        if failure == "rate_limit"
        else requests.ConnectionError("temporary failure secret-key")
    )
    session = FakeSession(default=answer)
    database = tmp_path / "market.sqlite"
    error = EdinetRateLimitError if failure == "rate_limit" else EdinetAcquisitionError
    with (
        closing(EDINETRefresh("secret-key", sqlite_path=database, session=session)) as provider,
        pytest.raises(error) as caught,
    ):
        provider.download_csv_zip("S100TEST")
    assert len(session.calls) == 6
    assert "secret-key" not in str(caught.value)
    assert not native_owner.freeze().archive_keys
    assert not database.exists()
    assert not (tmp_path / "edinet/csv_zips/S100TEST.zip").exists()


@pytest.mark.parametrize("body", [b"invalid zip", b'{"StatusCode":401}', b'{"StatusCode":403}'])
def test_non_zip_csv_response_is_not_retried_or_published(tmp_path, native_owner, body):
    session = FakeSession(response(body))
    database = tmp_path / "market.sqlite"
    with (
        closing(EDINETRefresh("key", sqlite_path=database, session=session)) as provider,
        pytest.raises(EDINETProviderError, match="not a ZIP"),
    ):
        provider.download_csv_zip("S100TEST")
    assert len(session.calls) == 1
    assert not native_owner.freeze().archive_keys
    assert not database.exists()


def test_edinet_refresh_requires_api_key_for_unretained_archive(tmp_path, native_owner):
    with (
        closing(EDINETRefresh(None, sqlite_path=tmp_path / "market.sqlite")) as provider,
        pytest.raises(EdinetAcquisitionError, match="EDINET_API_KEY"),
    ):
        provider.download_csv_zip("S100TEST")
    assert not native_owner.freeze().archive_keys
