"""新native raw wireのreader先行準備。通常publisherはまだ呼び出さない。"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime

import pyarrow.parquet as pq
import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.archive_native import (
    HEAD_KEY as ARCHIVE_HEAD,
)
from baibai_engine.data_collection.providers.edinet.archive_native import (
    ArchiveMetadata,
    EdinetArchiveL1,
    FixedEdinetArchives,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.facts_scope import FactsScope, read_scope
from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY as LIST_HEAD
from baibai_engine.data_collection.providers.edinet.list_native import EdinetListL1
from baibai_engine.machine_analysis.generation.edinet_inputs import (
    read_edinet_input,
    retain_edinet_input,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import (
    LakeHydrateError,
    dehydrate_market_store,
    hydrate_market_store,
)
from baibai_engine.storage.snapshots.models import (
    DatasetManifest,
    NativeL1SourceRef,
    SQLiteSnapshotSourceRef,
    load_lake_model_json,
)
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import materialize_partitions, resolve_release
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import (
    LakeBuildError,
    export_market_store,
    validate_snapshot_parity,
)
from baibai_engine.storage.sqlite.source_cache.lake_origin import read_lake_store_origin
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.edinet_archive_native import (
    materialize_archives,
    rebuild_fixed_edinet_input,
)
from baibai_jobs.publication import l1_edinet_archive_export as adapter
from baibai_jobs.publication.l1_edinet_archive_export import DATASET
from baibai_jobs.publication.policy import release_policy_for_profile

AT = datetime(2026, 11, 7, 2, tzinfo=UTC)
SELECT = "SELECT doc_id,source_type,stored_on,zip_content FROM edinet_source_archives"


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    narrow_release_policy(monkeypatch, datasets=(DATASET.name,), carries_history=None)


def setup(tmp_path):
    store = MemoryR2Store()
    lists = EdinetListL1(store, tmp_path / "lists").freeze(require_existing=False)
    owner = EdinetArchiveL1(store, tmp_path / "archives")
    owner.initialize()
    originals = []
    for doc_id, kind, day, clock in (
        ("S0000001", 1, date(2026, 9, 30), None),
        ("S0000001", 5, date(2026, 10, 1), datetime(2026, 10, 1, 0, 1, tzinfo=UTC)),
        ("S0000002", 1, date(2026, 10, 1), None),
        ("S0000003", 1, date(2026, 10, 2), None),
    ):
        # Hash-valid raw originals need not be parseable ZIPs.
        body = b"opaque\x00" + hashlib.shake_256(f"{doc_id}:{kind}".encode()).digest(65536)
        originals.append(
            NativeArchive(
                ArchiveMetadata(
                    doc_id=doc_id,
                    source_type=kind,
                    stored_on=day,
                    received_at_utc=clock,
                    sha256=hashlib.sha256(body).hexdigest(),
                    size=len(body),
                ),
                body,
            )
        )
    archives = owner.publish(
        originals,
        scope=FactsScope(
            since=date(2026, 9, 1),
            doc_ids=frozenset({"S0000999"}),
            legacy_origins=frozenset({"b" * 64}),
        ),
    )
    saved = retain_edinet_input(store, lists, archives, selected_at=AT)
    database = tmp_path / "oracle.sqlite"
    rebuild_fixed_edinet_input(lists, archives, database)
    return store, owner, saved, database


def rows(database):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(SELECT + " ORDER BY doc_id,source_type").fetchall()


def export(store, saved, mirror, identity="native"):
    return adapter.export_edinet_archives_from_native_input(
        store=store,
        saved=saved,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        build_id=identity,
        created_at=AT,
    )


def release(build, mirror, identity):
    path, _ = create_release(
        dataset_manifest_paths=[build.manifest_path],
        mirror_root=mirror,
        release_id=identity,
        created_at=AT,
        catalog={DATASET.name: DATASET},
        policy=release_policy_for_profile("production"),
    )
    return resolve_release(
        LocalMirrorSource(mirror),
        identity,
        manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def test_saved_pair_exports_exact_rows_once_without_heads_or_sql(tmp_path):
    store, owner, saved, database = setup(tmp_path)
    fixed = read_edinet_input(store, saved)
    original = database.read_bytes()
    # Later heads and archives cannot expand the supplied selection.
    extra = b"later"
    owner.publish(
        [
            NativeArchive(
                ArchiveMetadata(
                    doc_id="S0000004",
                    source_type=5,
                    stored_on=date(2026, 11, 7),
                    sha256=hashlib.sha256(extra).hexdigest(),
                    size=len(extra),
                ),
                extra,
            )
        ]
    )
    store.values.pop(ARCHIVE_HEAD)
    store.values.pop(LIST_HEAD, None)
    store.get_keys.clear()
    store.put_keys.clear()
    mirror = tmp_path / "mirror"
    report = export(store, saved, mirror)
    manifest = report.manifest
    assert manifest.totals.rows == 4
    assert manifest.totals.objects == 3
    assert manifest.coverage_start == date(2026, 9, 30)
    assert manifest.data_as_of == date(2026, 10, 2)
    assert manifest.coverage_status == "partial"
    assert manifest.population_count is None
    digest = saved.descriptor["selection_sha256"]
    assert {source for part in manifest.partitions for source in part.sources} == {
        NativeL1SourceRef(
            kind="native_l1",
            source_id="native-l1-" + digest[:24],
            sha256=digest,
            namespace="edinet",
            key=f"fixed-inputs/edinet/v1/selections/{digest}.json",
        )
    }
    assert ARCHIVE_HEAD not in store.get_keys
    assert LIST_HEAD not in store.get_keys
    assert len([key for key in store.get_keys if key.endswith(".zip")]) == 4
    assert store.put_keys == []
    validate_snapshot_parity(
        dataset=DATASET, sqlite_path=database, mirror_root=mirror, manifest=manifest
    )
    for part in manifest.partitions:
        for obj in part.objects:
            with pq.ParquetFile(mirror / obj.key, pre_buffer=False) as parquet:
                assert parquet.schema_arrow.equals(
                    DATASET.arrow_schema_for("l1_derived"), check_metadata=True
                )
                assert parquet.metadata.num_row_groups == obj.rows
                assert all(parquet.metadata.row_group(i).num_rows == 1 for i in range(obj.rows))
    with closing(open_connection(database)) as connection:
        assert read_scope(connection) == fixed.archives.scope
    assert database.read_bytes() == original
    # Same content reuses existing immutable objects despite a new build identity.
    repeated = export(store, saved, mirror, "repeated")
    assert repeated.new_objects == 0
    assert repeated.manifest.partitions == manifest.partitions


def test_old_sqlite_months_and_new_native_days_query_hydrate_and_dehydrate(tmp_path):
    store, _, saved, database = setup(tmp_path)
    mirror = tmp_path / "mirror"
    legacy = replace(DATASET, object_date_grain=None)
    old = export_market_store(
        catalog={DATASET.name: legacy},
        sqlite_path=database,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        expected_store_origin=None,
        created_at=AT,
    ).datasets[DATASET.name]
    native = export(store, saved, mirror)
    assert old.manifest.totals.objects == 2
    assert all(
        source.kind == "sqlite_snapshot"
        for part in old.manifest.partitions
        for source in part.sources
    )
    for build in (old, native):
        fixed = release(build, mirror, build.manifest.build_id)
        cache = LakeObjectCache(root=tmp_path / fixed.release_id, source=LocalMirrorSource(mirror))
        with lake_session() as session:
            paths = materialize_partitions(
                cache, dataset=DATASET, partitions=build.manifest.partitions
            )
            actual = session.connection.execute(
                "SELECT doc_id,source_type,stored_on,zip_content FROM read_parquet($files) "
                "ORDER BY doc_id,source_type",
                {"files": [str(path) for path in paths]},
            ).fetchall()
            assert actual == rows(database)
            target = tmp_path / (fixed.release_id + ".sqlite")
            open_connection(target).close()
            with pytest.raises(LakeHydrateError, match="origin does not match"):
                dehydrate_market_store(target, catalog={DATASET.name: DATASET}, release=fixed)
            for _ in range(2):
                report = hydrate_market_store(
                    session,
                    catalog={DATASET.name: DATASET},
                    release=fixed,
                    cache=cache,
                    store=target,
                    dataset_names=(DATASET.name,),
                )
                assert report.rows == {DATASET.name: 4}
                assert rows(target) == rows(database)
                assert read_lake_store_origin(target).release_id == fixed.release_id
                with closing(sqlite3.connect(target)) as connection, connection:
                    connection.execute("DELETE FROM edinet_source_archives WHERE source_type=5")
                with pytest.raises(LakeHydrateError, match="publish before emptying"):
                    dehydrate_market_store(target, catalog={DATASET.name: DATASET}, release=fixed)
                hydrate_market_store(
                    session,
                    catalog={DATASET.name: DATASET},
                    release=fixed,
                    cache=cache,
                    store=target,
                    dataset_names=(DATASET.name,),
                )
                assert dehydrate_market_store(
                    target, catalog={DATASET.name: DATASET}, release=fixed
                ).removed_rows == {DATASET.name: 4}


@pytest.mark.parametrize("damage", ["missing", "hash", "size", "descriptor", "lists"])
def test_bad_fixed_original_never_uses_good_sql_or_publishes_a_manifest(tmp_path, damage):
    store, _, saved, database = setup(tmp_path)
    before = database.read_bytes()
    fixed = read_edinet_input(store, saved)
    key = sorted(key for key in store.values if key.endswith(".zip"))[0]
    if damage == "lists":
        store.values.pop(f"fixed-inputs/edinet/v1/lists/{fixed.lists.identity}.json")
    elif damage == "missing":
        store.values.pop(key)
    elif damage == "hash":
        value = store.values[key]
        store.store_object(key, b"x" * len(value.body), sha256=value.metadata["sha256"])
    elif damage == "size":
        head = fixed.archives.value()
        head["archives"][0]["size"] += 1
        saved = retain_edinet_input(
            store, fixed.lists, FixedEdinetArchives(store, head), selected_at=AT
        )
    else:
        key = f"fixed-inputs/edinet/v1/selections/{saved.descriptor['selection_sha256']}.json"
        value = store.values[key]
        store.store_object(key, value.body + b" ", sha256=value.metadata["sha256"])
    mirror = tmp_path / "failed"
    with pytest.raises(LakePublishError):
        export(store, saved, mirror)
    assert not list(mirror.rglob("*.parquet"))
    assert not (mirror / "lake/manifests").exists()
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "field", ["doc_id", "source_type", "stored_on", "zip_content", "omitted", "schema"]
)
def test_output_readback_checks_inventory_dates_and_content(tmp_path, monkeypatch, field):
    store, _, saved, _ = setup(tmp_path)
    build = adapter._build_period

    def damaged(**kwargs):
        if field == "schema":
            return build(
                **{
                    **kwargs,
                    "sources": (
                        SQLiteSnapshotSourceRef(
                            kind="sqlite_snapshot",
                            source_id="market-v1-" + "a" * 24,
                            sha256="a" * 64,
                            schema_version=1,
                            captured_at=AT,
                        ),
                    ),
                }
            )
        values = [list(row) for row in kwargs["rows"]]
        if field == "omitted":
            # Only damage a day containing two rows so the physical writer can finish.
            if len(values) > 1:
                values.pop()
        else:
            index = ("doc_id", "source_type", "stored_on", "zip_content").index(field)
            values[0][index] = ("S0000999", 5, "2026-09-29", b"wrong")[index]
        return build(**{**kwargs, "rows": (tuple(row) for row in values)})

    monkeypatch.setattr(adapter, "_build_period", damaged)
    with pytest.raises(LakeBuildError, match=r"schema differs|differs from fixed originals"):
        export(store, saved, tmp_path / "failed")
    assert not (tmp_path / "failed/lake/manifests").exists()


@pytest.mark.parametrize(
    "change", ["dataset", "namespace", "mixed", "selections", "derived", "source_id", "key"]
)
def test_native_raw_decoder_is_limited_to_one_exact_edinet_selection(tmp_path, change):
    store, _, saved, database = setup(tmp_path)
    mirror = tmp_path / "mirror"
    build = export(store, saved, mirror)
    value = build.manifest.model_dump(mode="json")
    source = value["partitions"][0]["sources"][0]
    if change == "dataset":
        value["dataset"] = "example.source"
    elif change == "namespace":
        for part in value["partitions"]:
            part["sources"][0] = {
                **source,
                "namespace": "example",
                "key": f"fixed-inputs/example/v1/selections/{source['sha256']}.json",
            }
    elif change in {"source_id", "key"}:
        value["partitions"][0]["sources"][0][change] += "wrong"
    elif change == "mixed":
        old = (
            export_market_store(
                catalog={DATASET.name: DATASET},
                sqlite_path=database,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                expected_store_origin=None,
                created_at=AT,
            )
            .datasets[DATASET.name]
            .manifest
        )
        value["partitions"][0]["sources"].append(
            old.partitions[0].sources[0].model_dump(mode="json")
        )
    elif change == "selections":
        alternate = copy.deepcopy(source)
        alternate.update(
            source_id="native-l1-" + "c" * 24,
            sha256="c" * 64,
            key=f"fixed-inputs/edinet/v1/selections/{'c' * 64}.json",
        )
        value["partitions"][1]["sources"] = [alternate]
    else:
        for part in value["partitions"]:
            part["sources"] = [
                {
                    "kind": "l1_dataset",
                    "source_id": "l1-" + "c" * 24,
                    "sha256": "c" * 64,
                    "dataset": "example.source",
                    "build_id": "source",
                    "key": "lake/manifests/datasets/example.source/source.json",
                }
            ]
    with pytest.raises(ValueError, match="invalid DatasetManifest"):
        load_lake_model_json(json.dumps(value), DatasetManifest)


def test_native_stored_on_correction_matches_finite_sql_adapter(tmp_path):
    store, _, saved, database = setup(tmp_path)
    fixed = read_edinet_input(store, saved)
    head = fixed.archives.value()
    corrected = head["archives"][0]
    corrected["stored_on"] = "2026-09-29"
    assert corrected["received_at_utc"] is None
    archives = FixedEdinetArchives(store, head)
    saved = retain_edinet_input(store, fixed.lists, archives, selected_at=AT)
    materialize_archives(archives, database)
    before = database.read_bytes()
    mirror = tmp_path / "mirror"
    native = export(store, saved, mirror)
    assert native.manifest.coverage_start == date(2026, 9, 29)
    validate_snapshot_parity(
        dataset=DATASET, sqlite_path=database, mirror_root=mirror, manifest=native.manifest
    )
    assert database.read_bytes() == before


def test_native_raw_rejects_empty_and_future_selection(tmp_path):
    store = MemoryR2Store()
    lists = EdinetListL1(store, tmp_path / "lists").freeze(require_existing=False)
    archives = EdinetArchiveL1(store, tmp_path / "archives").initialize()
    saved = retain_edinet_input(store, lists, archives, selected_at=AT)
    with pytest.raises(LakeBuildError, match="holds no rows"):
        export(store, saved, tmp_path / "empty")
    with pytest.raises(ValueError, match="predate"):
        adapter.export_edinet_archives_from_native_input(
            store=store,
            saved=saved,
            mirror_root=tmp_path / "future",
            producer_git_commit="a" * 40,
            created_at=AT.replace(hour=1),
        )


def test_explicit_export_clock_cannot_silently_use_host_timezone(tmp_path):
    store, _, saved, _ = setup(tmp_path)
    store.get_keys.clear()
    with pytest.raises(ValueError, match="requires a timezone"):
        adapter.export_edinet_archives_from_native_input(
            store=store,
            saved=saved,
            mirror_root=tmp_path / "naive",
            producer_git_commit="a" * 40,
            created_at=AT.replace(tzinfo=None),
        )
    assert store.get_keys == []
