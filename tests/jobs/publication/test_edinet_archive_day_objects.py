"""EDINET原本の意味を変えず、変更した受領日の物理objectだけを更新する。"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.price_native import native_price_publication as native_price_publication
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.snapshot_contracts import (
    EDINET_SOURCE_ARCHIVES as DATASET,
)
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import dehydrate_market_store, hydrate_market_store
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import (
    materialize_partitions,
    resolve_release,
    selected_partitions,
)
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_market_store
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    advance_lake_store_origin,
    read_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication import snapshot_publication as publication
from baibai_jobs.publication.policy import release_policy_for_profile
from baibai_jobs.publication.snapshot_release import publish_release

LEGACY = replace(DATASET, object_date_grain=None)
AT = datetime(2026, 11, 7, tzinfo=UTC)
SELECT = "SELECT doc_id,source_type,stored_on,zip_content"


def insert(database, number, day):
    with closing(open_connection(database)) as connection, connection:
        connection.execute(
            "INSERT INTO edinet_source_archives VALUES (?,?,?,?)",
            (
                f"S{number:07d}",
                1 if number % 2 else 5,
                day,
                hashlib.shake_256(str(number).encode()).digest(65536),
            ),
        )


def rows(database):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(
            SELECT + " FROM edinet_source_archives ORDER BY doc_id,source_type"
        ).fetchall()


def build(database, mirror, identity, dataset):
    export = export_market_store(
        catalog={DATASET.name: dataset},
        sqlite_path=database,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        expected_store_origin=read_lake_store_origin(database),
        created_at=AT,
    )
    path, _ = create_release(
        dataset_manifest_paths=[export.datasets[DATASET.name].manifest_path],
        mirror_root=mirror,
        release_id=identity,
        created_at=AT,
        catalog={DATASET.name: dataset},
        policy=release_policy_for_profile("production"),
    )
    return path, resolve_release(
        LocalMirrorSource(mirror),
        identity,
        manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    narrow_release_policy(monkeypatch, datasets=(DATASET.name,), carries_history=None)
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)


def test_old_catalog_reads_new_days_and_new_catalog_reads_old_months(tmp_path):
    database, mirror = tmp_path / "market.sqlite", tmp_path / "mirror"
    for number, day in enumerate(("2026-09-30", "2026-10-01", "2026-10-02"), 1):
        insert(database, number, day)
    original = database.read_bytes()
    old_path, old = build(database, mirror, "old-months", LEGACY)
    new_path, new = build(database, mirror, "new-days", DATASET)
    store = MemoryR2Store()
    for path in (old_path, new_path):
        publish_release(mirror_root=mirror, release_manifest_path=path, store=store)
    assert old.dataset_manifest(DATASET.name).totals.objects == 2
    assert new.dataset_manifest(DATASET.name).totals.objects == 3
    assert DATASET.arrow_schema.equals(LEGACY.arrow_schema, check_metadata=True)
    assert (
        hashlib.sha256(DATASET.arrow_schema.serialize()).digest()
        == hashlib.sha256(LEGACY.arrow_schema.serialize()).digest()
    )
    source_hashes = {
        source.sha256
        for part in old.dataset_manifest(DATASET.name).partitions
        for source in part.sources
    }
    assert len(source_hashes) == 1
    for release, reader in ((new, LEGACY), (old, DATASET)):
        manifest = release.dataset_manifest(DATASET.name)
        assert manifest.partition_by == ("year", "month")
        assert manifest.contract_version == 1
        assert {
            source.sha256 for part in manifest.partitions for source in part.sources
        } == source_hashes
        store.get_keys.clear()
        cache = LakeObjectCache(
            root=tmp_path / ("cold-" + release.release_id),
            source=SimpleNamespace(read_bytes=store.get_bytes),
        )
        with lake_session() as session:
            for start, periods in (
                ("2026-10-01", [(2026, 10)]),
                ("2026-09-30", [(2026, 9), (2026, 10)]),
            ):
                paths = materialize_partitions(
                    cache,
                    dataset=reader,
                    partitions=selected_partitions(release, DATASET.name, periods=periods),
                )
                actual = session.connection.execute(
                    SELECT
                    + " FROM read_parquet($files,union_by_name=false) WHERE stored_on >= $start AND stored_on <= '2026-10-01' ORDER BY doc_id,source_type",
                    {"files": [str(path) for path in paths], "start": start},
                ).fetchall()
                assert actual == [row for row in rows(database) if start <= row[2] <= "2026-10-01"]
            target = tmp_path / (release.release_id + ".sqlite")
            open_connection(target).close()
            for _ in range(2):
                hydrate_market_store(
                    session,
                    catalog={DATASET.name: reader},
                    release=release,
                    cache=cache,
                    store=target,
                    dataset_names=(DATASET.name,),
                )
                assert rows(target) == rows(database)
                assert dehydrate_market_store(
                    target, catalog={DATASET.name: reader}, release=release
                ).removed_rows == {DATASET.name: 3}
        assert (
            len([key for key in store.get_keys if key.endswith(".parquet")])
            == manifest.totals.objects
        )
    assert database.read_bytes() == original


@pytest.mark.parametrize("concentrated", [False, True])
def test_publication_reuses_old_months_and_only_changes_receipt_day_objects(tmp_path, concentrated):
    database, mirror = tmp_path / "market.sqlite", tmp_path / "initial"
    insert(database, 1, "2026-09-30")
    for number in range(2, 8):
        day = 1 if concentrated else (number - 2) // 2 + 1
        insert(database, number, f"2026-10-{day:02d}")
    path, old = build(database, mirror, "legacy", LEGACY)
    store = MemoryR2Store()
    publish_release(mirror_root=mirror, release_manifest_path=path, store=store)
    old_bytes = {
        key: value.body
        for key, value in store.values.items()
        if not key.startswith("lake/pointers/")
    }
    advance_lake_store_origin(
        database, expected=None, target=LakeStoreOrigin(old.release_id, old.manifest_sha256)
    )
    previous_keys = {
        obj.key for part in old.dataset_manifest(DATASET.name).partitions for obj in part.objects
    }
    steps = [
        (None, 7, 0),
        ("2026-10-04", 8, 1 if concentrated else 4),
        ("2026-10-05", 9, 1),
        ("2026-10-02", 10, 1),
        ("move", 10, 0 if concentrated else 2),
    ]
    for index, (day, count, expected_puts) in enumerate(steps):
        if day == "move":
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "UPDATE edinet_source_archives SET stored_on='2026-10-01' WHERE doc_id='S0000006'"
                )
        elif day:
            insert(database, 7 + index, "2026-10-01" if concentrated else day)
        store.put_keys.clear()
        with publication.prepare_market_lake(
            sqlite_path=database,
            mirror_root=tmp_path / f"fresh-{index}",
            store=store,
            release_id=f"daily-{index}",
        ) as prepared:
            _, monthly = build(
                prepared.source_snapshot.path,
                tmp_path / f"monthly-{index}",
                f"monthly-{index}",
                LEGACY,
            )
            monthly_bytes = sum(
                obj.bytes for obj in monthly.dataset_manifest(DATASET.name).partitions[1].objects
            )
            manifest = prepared.export.datasets[DATASET.name].manifest
            assert (
                manifest.partitions[0].objects
                == old.dataset_manifest(DATASET.name).partitions[0].objects
            )
            assert manifest.totals.rows == count
            assert all(
                part.sources == (prepared.source_snapshot.ref,) for part in manifest.partitions
            )
            assert (
                prepared.source_snapshot.ref.sha256
                == monthly.dataset_manifest(DATASET.name).partitions[0].sources[0].sha256
            )
            publication.publish_prepared_market_lake(prepared, store=store)
        puts = [key for key in store.put_keys if key.endswith(".parquet")]
        assert len(puts) == expected_puts
        current_keys = {obj.key for part in manifest.partitions for obj in part.objects}
        assert set(puts) == current_keys - previous_keys
        previous_keys = current_keys
        added_bytes = sum(len(store.values[key].body) for key in puts)
        if expected_puts:
            if concentrated:
                assert added_bytes == monthly_bytes
            elif index == 1:
                assert added_bytes > monthly_bytes  # 初回だけ変更月の旧bodyを日単位へ再配置する。
            else:
                assert added_bytes < monthly_bytes
        else:
            assert added_bytes == 0
        assert all(store.values[key].body == body for key, body in old_bytes.items())


pytestmark = pytest.mark.usefixtures("native_price_publication")
