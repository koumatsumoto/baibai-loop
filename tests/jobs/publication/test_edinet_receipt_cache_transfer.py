"""旧releaseと空local copyはnative原受領の検証接続を必要としない。"""

from contextlib import closing
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication.snapshot_write_cli import _clear_independent_list_cache


@pytest.mark.parametrize("existing", [False, True])
def test_empty_local_copy_needs_no_bucket_or_native_head(tmp_path, monkeypatch, existing):
    database = tmp_path / "market.sqlite"
    if existing:
        with closing(open_connection(database)):
            pass
    remote = Mock(side_effect=AssertionError("empty cache must not connect to R2"))
    monkeypatch.setattr("baibai_engine.storage.objects.r2.Boto3R2Store", remote)
    release = SimpleNamespace(dataset_names=lambda: ("jquants.fin_summaries",))
    args = SimpleNamespace(store=database, mirror=tmp_path / "mirror", bucket=None)
    _clear_independent_list_cache(args, release)
    assert database.exists() is existing
    remote.assert_not_called()


@pytest.mark.parametrize("legacy_release", [False, True])
def test_nonempty_cache_requires_native_verification_only_when_release_omits_originals(
    tmp_path, monkeypatch, legacy_release
):
    database = tmp_path / "market.sqlite"
    with closing(open_connection(database)) as connection, connection:
        connection.execute(
            "INSERT INTO edinet_list_responses VALUES(?,?,?,?)",
            ("retained-receipt", "2026-11-01", "2026-11-01T00:00:00+00:00", b"original"),
        )
    original = database.read_bytes()
    remote = Mock(side_effect=AssertionError("missing bucket must not connect to R2"))
    monkeypatch.setattr("baibai_engine.storage.objects.r2.Boto3R2Store", remote)
    release = SimpleNamespace(
        dataset_names=lambda: ("edinet.list_responses",) if legacy_release else ()
    )
    args = SimpleNamespace(store=database, mirror=tmp_path / "mirror", bucket=None)
    if legacy_release:
        _clear_independent_list_cache(args, release)
    else:
        with pytest.raises(ValueError, match="requires --bucket"):
            _clear_independent_list_cache(args, release)
    assert database.read_bytes() == original
    remote.assert_not_called()


def test_native_cache_clear_uses_the_original_receipt_proof(tmp_path, monkeypatch):
    store = MemoryR2Store()
    store.close = Mock()
    lists = EdinetListL1(store, tmp_path / "lists")
    lists.freeze(False)
    receipt = NativeListReceipt.from_response(
        date(2026, 11, 1), EdinetResponse(datetime(2026, 11, 1, tzinfo=UTC), b"{}")
    )
    lists.publish((receipt,))
    database = tmp_path / "market.sqlite"
    with closing(open_connection(database)) as connection, connection:
        connection.execute(
            "INSERT INTO edinet_list_responses VALUES(?,?,?,?)",
            (
                receipt.receipt_id,
                receipt.doc_date.isoformat(),
                receipt.received_at.isoformat(timespec="microseconds"),
                receipt.body_gzip,
            ),
        )
    monkeypatch.setattr("baibai_engine.storage.objects.r2.Boto3R2Store", lambda **_: store)
    args = SimpleNamespace(store=database, mirror=tmp_path / "mirror", bucket="fixture")
    _clear_independent_list_cache(args, SimpleNamespace(dataset_names=lambda: ()))
    with closing(open_connection(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM edinet_list_responses").fetchone() == (0,)
    store.close.assert_called_once_with()
