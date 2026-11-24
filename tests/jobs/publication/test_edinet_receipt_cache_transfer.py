"""旧releaseと空local copyはnative原受領の検証接続を必要としない。"""

from contextlib import closing
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.helpers.lake_release import lake as lake
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.storage.snapshots import hydrate
from baibai_engine.storage.snapshots.keys import current_release_pointer_key, release_manifest_key
from baibai_engine.storage.snapshots.models import canonical_lake_model_bytes
from baibai_engine.storage.snapshots.objects import LocalMirrorSource, open_lake
from baibai_engine.storage.snapshots.prefetch import prefetching_hydration_cache
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.snapshots.release import LakeReleasePointer
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication import snapshot_write_cli
from baibai_jobs.publication.snapshot_write_cli import _clear_independent_list_cache


@pytest.mark.parametrize("existing", [False, True])
def test_empty_local_copy_needs_no_bucket_or_native_head(tmp_path, monkeypatch, existing, capsys):
    database = tmp_path / "market.sqlite"
    if existing:
        with closing(open_connection(database)):
            pass
    remote = Mock(side_effect=AssertionError("empty cache must not connect to R2"))
    monkeypatch.setattr("baibai_engine.storage.objects.r2.Boto3R2Store", remote)
    release = SimpleNamespace(dataset_names=lambda: ("jquants.fin_summaries",))
    _clear_independent_list_cache(
        release, path=database, mirror=tmp_path / "mirror", bucket=None, timed=False
    )
    assert database.exists() is existing
    remote.assert_not_called()
    assert capsys.readouterr().err == ""


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
    mirror = tmp_path / "mirror"
    if legacy_release:
        _clear_independent_list_cache(
            release, path=database, mirror=mirror, bucket=None, timed=False
        )
    else:
        with pytest.raises(ValueError, match="requires --bucket"):
            _clear_independent_list_cache(
                release, path=database, mirror=mirror, bucket=None, timed=False
            )
    assert database.read_bytes() == original
    remote.assert_not_called()


@pytest.mark.parametrize("mode", ["working-store", "transfer-copy", "broken-stderr"])
def test_native_cache_clear_uses_the_original_receipt_proof(tmp_path, monkeypatch, capfd, mode):
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
    with monkeypatch.context() as log_context:
        if mode == "broken-stderr":
            write = Mock(side_effect=OSError("diagnostic stream unavailable"))
            log_context.setattr("baibai_jobs.publication.snapshot_write_cli.os.write", write)
        _clear_independent_list_cache(
            SimpleNamespace(dataset_names=lambda: ()),
            path=database,
            mirror=tmp_path / "mirror",
            bucket="fixture",
            timed=mode != "working-store",
        )
        if mode == "broken-stderr":
            write.assert_called_once()
    with closing(open_connection(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM edinet_list_responses").fetchone() == (0,)
    store.close.assert_called_once_with()
    output = capfd.readouterr()
    assert output.out == ""
    if mode == "transfer-copy":
        assert output.err.startswith("snapshot: phase=verify_native_edinet_list_cache state=done ")
        assert output.err.strip().endswith(" verified_rows=1")
    else:
        assert output.err == ""


@pytest.mark.parametrize("command", ["hydrate", "hydrate-for-daily"])
@pytest.mark.parametrize("failure", [None, "parquet", "current-moved", "space", "unpublished"])
def test_hydrate_installs_verified_native_cache_clear_only_with_the_release(
    tmp_path, monkeypatch, capsys, lake, command, failure
):
    mirror, pointer = lake
    native = MemoryR2Store()
    native.close = Mock()
    owner = EdinetListL1(native, tmp_path / "native")
    owner.freeze(False)
    receipt = NativeListReceipt.from_response(
        date(2026, 1, 5), EdinetResponse(datetime(2026, 1, 5, tzinfo=UTC), b"{}")
    )
    owner.publish((receipt,))
    database = tmp_path / "working.sqlite"
    held = (
        NativeListReceipt.from_response(
            receipt.doc_date, EdinetResponse(datetime(2026, 1, 5, 1, tzinfo=UTC), b"{}")
        )
        if failure == "unpublished"
        else receipt
    )
    with closing(open_connection(database)) as connection, connection:
        connection.execute(
            "INSERT INTO edinet_list_responses VALUES (?, ?, ?, ?)",
            (
                held.receipt_id,
                held.doc_date.isoformat(),
                held.received_at.isoformat(timespec="microseconds"),
                held.body_gzip,
            ),
        )
    before = database.read_bytes()
    originals = {key: item.body for key, item in native.values.items()}
    native.put_keys.clear()

    def open_fixture(*, mirror, bucket):
        assert bucket == "offline-fixture"
        return open_lake(mirror=mirror)

    def prefetch_fixture(cache, *, release, dataset_names, bucket):
        assert bucket == "offline-fixture"
        return prefetching_hydration_cache(
            cache, release=release, dataset_names=dataset_names, bucket=None
        )

    monkeypatch.setattr(snapshot_write_cli, "open_lake", open_fixture)
    monkeypatch.setattr(snapshot_write_cli, "prefetching_hydration_cache", prefetch_fixture)
    monkeypatch.setattr("baibai_engine.storage.objects.r2.Boto3R2Store", lambda **_: native)

    current = []
    recheck = snapshot_write_cli.recheck_current_release

    def verify_current(source, release):
        if failure == "current-moved" and len(current) == 1:
            moved = LakeReleasePointer(
                release_id="a-distinct-release",
                manifest_key=release_manifest_key(release_id="a-distinct-release"),
                manifest_sha256="f" * 64,
            )
            (mirror / current_release_pointer_key()).write_bytes(canonical_lake_model_bytes(moved))
        result = recheck(source, release)
        current.append(result)
        return result

    monkeypatch.setattr(snapshot_write_cli, "recheck_current_release", verify_current)
    if failure == "parquet":
        release = resolve_release(
            LocalMirrorSource(mirror), pointer.release_id, manifest_sha256=pointer.manifest_sha256
        )
        item = release.dataset_manifest("jquants.daily_bars").partitions[0].objects[0]
        (mirror / item.key).write_bytes(b"intentionally invalid local object")
    if failure == "space":
        monkeypatch.setattr(
            hydrate, "shutil", SimpleNamespace(disk_usage=lambda _: SimpleNamespace(free=0))
        )

    status = snapshot_write_cli.main(
        [
            command,
            "--mirror",
            str(mirror),
            "--store",
            str(database),
            "--bucket",
            "offline-fixture",
        ]
    )
    assert status == int(failure is not None)
    output = capsys.readouterr()
    assert native.put_keys == []
    assert {key: item.body for key, item in native.values.items()} == originals
    assert not list(tmp_path.glob(".working.sqlite.*.hydrating*"))
    if failure is not None:
        assert output.out == ""
        assert read_lake_store_origin(database) is None
        with closing(open_connection(database)) as connection:
            assert connection.execute(
                "SELECT receipt_id FROM edinet_list_responses"
            ).fetchall() == [(held.receipt_id,)]
        assert database.read_bytes() == before
    else:
        assert read_lake_store_origin(database) == LakeStoreOrigin(
            pointer.release_id, pointer.manifest_sha256
        )
        with closing(open_connection(database)) as connection:
            assert not connection.execute("SELECT 1 FROM edinet_list_responses").fetchall()
            assert connection.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone() == (4,)
    if failure == "unpublished":
        assert "unpublished originals" in output.err
        assert len(current) < 2
    elif failure == "parquet":
        assert "cached lake object differs" in output.err
        assert item.key in output.err
        assert len(current) == 1
    elif failure == "current-moved":
        assert "current release moved" in output.err
        assert len(current) == 2
    elif failure == "space":
        assert "hydration requires at least " in output.err
        assert len(current) == 1
    else:
        assert len(current) == 2
    if failure == "space":
        native.close.assert_not_called()
    else:
        native.close.assert_called_once_with()
