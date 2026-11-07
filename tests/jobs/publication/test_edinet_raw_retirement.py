"""通常固定pairは原本をnativeに保ち、旧raw配布とSQL copyだけを退役させる。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.jobs.integration.test_edinet_fixed_consumer_input import fixtures

from baibai_engine.data_collection.providers.edinet.archive_native import HEAD_KEY as ARCHIVE_HEAD
from baibai_engine.data_collection.providers.edinet.facts_scope import (
    FactsScope,
    merge_scope,
    read_scope,
)
from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY as LIST_HEAD
from baibai_engine.machine_analysis.generation.edinet_inputs import retain_edinet_input
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import (
    LakeHydrateError,
    dehydrate_market_store,
    hydrate_market_store,
)
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.sqlite.source_cache.lake_origin import read_lake_store_origin
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.edinet_archive_native import (
    prepare_archive_execution,
    project_archive_execution,
    rebuild_fixed_edinet_input,
)
from baibai_jobs.publication import snapshot_publication as publication
from baibai_jobs.publication.datasets import LAKE_DATASETS

RAW = "edinet.source_archives"
DOCUMENTS = "edinet.documents"
POINTER = "lake/pointers/market/current.json"


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    names = (RAW, DOCUMENTS)
    narrow_release_policy(monkeypatch, datasets=names, carries_history=None)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {name: LAKE_DATASETS[name] for name in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    store, _, _, lists, archives, _ = fixtures(tmp_path)
    market = tmp_path / "market.sqlite"
    rebuild_fixed_edinet_input(lists, archives, market)
    mirror = tmp_path / "mirror"
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, release_id="legacy"
    ) as prepared:
        old = prepared
        publication.publish_prepared_market_lake(prepared, store=store)
    saved = retain_edinet_input(store, lists, archives, selected_at=datetime.now(UTC))
    del store.values[ARCHIVE_HEAD]
    del store.values[LIST_HEAD]
    store.get_keys.clear()
    store.head_keys.clear()
    store.put_keys.clear()
    return store, lists, archives, saved, market, mirror, old


def archive_count(market):
    with sqlite3.connect(market) as connection:
        return connection.execute("SELECT COUNT(*) FROM edinet_source_archives").fetchone()[0]


def test_normal_publication_omits_raw_after_fixed_metadata_proof_and_preserves_legacy(
    legacy, tmp_path
):
    store, _, archives, saved, market, mirror, old = legacy
    before = {key: value.body for key, value in store.values.items() if key != POINTER}
    project_archive_execution(archives, market)
    assert archive_count(market) == len(
        archives.archive_keys
    )  # finite old-release copy, no new ZIPs
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, release_id="native", edinet_input=saved
    ) as prepared:
        assert set(prepared.export.datasets) == {DOCUMENTS}
        assert archive_count(market) == 0
        with sqlite3.connect(market) as connection:
            assert read_scope(connection) == archives.scope
        publication.publish_prepared_market_lake(prepared, store=store)
        current = resolve_release(
            LocalMirrorSource(mirror), "native", manifest_sha256=prepared.release_sha256
        )
    assert all(store.values[key].body == payload for key, payload in before.items())
    assert not any(key.endswith(".zip") for key in store.get_keys)
    assert ARCHIVE_HEAD not in store.head_keys + store.get_keys
    assert LIST_HEAD not in store.head_keys + store.get_keys
    assert not any(RAW in key for key in store.put_keys)
    with publication.prepare_market_lake(
        sqlite_path=market,
        mirror_root=mirror,
        store=store,
        release_id="native-next",
        edinet_input=saved,
    ) as next_prepared:
        assert RAW not in next_prepared.export.datasets
        assert archive_count(market) == 0

    # No special omission/origin/rowcount hook is needed: raw is absent from the new release.
    catalog = {name: LAKE_DATASETS[name] for name in (RAW, DOCUMENTS)}
    target = tmp_path / "execution.sqlite"
    with closing(open_connection(target)):
        pass
    with lake_session() as session:
        hydrate_market_store(
            session,
            catalog=catalog,
            release=current,
            cache=LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
            store=target,
            dataset_names=current.dataset_names(),
        )
    assert archive_count(target) == 0
    assert read_lake_store_origin(target).release_id == "native"
    dehydrate_market_store(target, catalog=catalog, release=current)

    # The immutable old release remains an explicit full SQL recovery adapter.
    old_release = resolve_release(
        LocalMirrorSource(mirror), "legacy", manifest_sha256=old.release_sha256
    )
    with lake_session() as session:
        hydrate_market_store(
            session,
            catalog=catalog,
            release=old_release,
            cache=LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
            store=target,
            dataset_names=old_release.dataset_names(),
        )
    assert archive_count(target) == len(archives.archive_keys)
    assert dehydrate_market_store(target, catalog=catalog, release=old_release).removed_rows[
        RAW
    ] == len(archives.archive_keys)


@pytest.mark.parametrize("broken", ["bytes", "unknown", "earlier_day", "scope"])
def test_retirement_rejects_unproven_copy_before_clear_floor_or_pointer(legacy, broken):
    store, _, _, saved, market, mirror, _ = legacy
    with sqlite3.connect(market) as connection:
        if broken == "bytes":
            connection.execute("UPDATE edinet_source_archives SET zip_content=x'aa'")
        elif broken == "unknown":
            connection.execute(
                "INSERT INTO edinet_source_archives VALUES('S100OTHER',1,'2026-11-02',x'aa')"
            )
        elif broken == "earlier_day":
            connection.execute("UPDATE edinet_source_archives SET stored_on='2026-11-01'")
        elif broken == "scope":
            merge_scope(connection, FactsScope(doc_ids=frozenset({"S100OTHER"})))
    before = market.read_bytes()
    pointer = store.values[POINTER]
    with (
        pytest.raises(ValueError, match="fixed native head"),
        publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, edinet_input=saved
        ),
    ):
        pytest.fail("unproven copy must not prepare a release")
    assert market.read_bytes() == before
    assert store.values[POINTER] == pointer
    assert store.put_keys == []
    assert not any(key.endswith(".zip") for key in store.get_keys)


def test_fixed_pair_does_not_authorize_a_wrong_serving_origin(legacy):
    _, _, archives, saved, market, mirror, _ = legacy
    # Native proof does not authorize a wrong origin, even when every ZIP matches.
    with sqlite3.connect(market) as connection:
        connection.execute("DELETE FROM lake_store_origin")
    before = market.read_bytes()
    from baibai_engine.storage.objects.r2 import LakePublishError

    with (
        pytest.raises(LakePublishError, match="serving store origin"),
        publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=legacy[0], edinet_input=saved
        ),
    ):
        pytest.fail("wrong origin must stop")
    assert market.read_bytes() == before
    assert archive_count(market) == len(archives.archive_keys)


def test_empty_normal_execution_keeps_scope_without_reading_or_copying_zip(legacy, tmp_path):
    store, _, archives, _, _, _, _ = legacy
    output = tmp_path / "empty.sqlite"
    report = project_archive_execution(archives, output)
    assert report["copied_archives"] == 0
    assert prepare_archive_execution(archives, output)["cleared_archives"] == 0
    assert archive_count(output) == 0
    with sqlite3.connect(output) as connection:
        assert read_scope(connection) == archives.scope
        assert "S100MISSING" in read_scope(connection).doc_ids
    assert store.get_keys == []


@pytest.mark.parametrize("failed_prepare", [False, True])
def test_empty_cold_or_failed_model_copy_retries_without_full_legacy_hydrate(
    legacy, failed_prepare
):
    store, _, archives, saved, market, mirror, _ = legacy
    pointer = store.values[POINTER]
    if failed_prepare:
        with (
            pytest.raises(RuntimeError, match="model failed"),
            publication.prepare_market_lake(
                sqlite_path=market,
                mirror_root=mirror,
                store=store,
                release_id="unpublished",
                edinet_input=saved,
            ),
        ):
            raise RuntimeError("model failed")
        assert store.values[POINTER] == pointer
    else:
        with sqlite3.connect(market) as connection:
            connection.execute("DELETE FROM edinet_source_archives")
    assert archive_count(market) == 0
    with publication.prepare_market_lake(
        sqlite_path=market,
        mirror_root=mirror,
        store=store,
        release_id="retry",
        edinet_input=saved,
    ) as prepared:
        assert RAW not in prepared.export.datasets
        publication.publish_prepared_market_lake(prepared, store=store)
    with sqlite3.connect(market) as connection:
        assert read_scope(connection) == archives.scope
    assert read_lake_store_origin(market).release_id == "retry"
    assert not any(key.endswith(".zip") for key in store.get_keys)


def test_raw_omission_does_not_weaken_existing_omitted_table_or_rowcount_guards(legacy, tmp_path):
    store, _, _, saved, market, mirror, _ = legacy
    with publication.prepare_market_lake(
        sqlite_path=market,
        mirror_root=mirror,
        store=store,
        release_id="native",
        edinet_input=saved,
    ) as prepared:
        publication.publish_prepared_market_lake(prepared, store=store)
        current = resolve_release(
            LocalMirrorSource(mirror), "native", manifest_sha256=prepared.release_sha256
        )
    catalog = {name: LAKE_DATASETS[name] for name in (RAW, DOCUMENTS)}
    target = tmp_path / "execution.sqlite"
    with closing(open_connection(target)) as connection, connection:
        connection.execute(
            "INSERT INTO edinet_source_archives VALUES('S100OTHER',1,'2026-11-02',x'aa')"
        )
    before = target.read_bytes()
    with lake_session() as session, pytest.raises(LakeHydrateError, match="does not publish"):
        hydrate_market_store(
            session,
            catalog=catalog,
            release=current,
            cache=LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
            store=target,
            dataset_names=current.dataset_names(),
        )
    assert target.read_bytes() == before
    with sqlite3.connect(target) as connection:
        connection.execute("DELETE FROM edinet_source_archives")
    with lake_session() as session:
        hydrate_market_store(
            session,
            catalog=catalog,
            release=current,
            cache=LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
            store=target,
            dataset_names=current.dataset_names(),
        )
    with sqlite3.connect(target) as connection:
        connection.execute(
            "INSERT INTO edinet_source_archives VALUES('S100OTHER',1,'2026-11-02',x'aa')"
        )
    with pytest.raises(LakeHydrateError, match="does not publish"):
        dehydrate_market_store(target, catalog=catalog, release=current)
    with sqlite3.connect(target) as connection:
        connection.execute("DELETE FROM edinet_source_archives")
        connection.execute("DELETE FROM edinet_documents WHERE sequence_number=1")
    with pytest.raises(LakeHydrateError, match="publish before emptying"):
        dehydrate_market_store(target, catalog=catalog, release=current)


def test_clear_compacts_only_legacy_raw_and_keeps_warm_ledger_clocks(legacy):
    store, _, archives, _, market, _, _ = legacy
    with sqlite3.connect(market) as connection:
        connection.execute(
            "INSERT INTO source_coverage VALUES (?,?,?,?,?,?,?,?)",
            (
                "edinet_research_facts",
                "successful-zero",
                "2026-10-01",
                "2026-10-01",
                "2026-11-02T03:00:00+00:00",
                0,
                "ok",
                '{"revision":"saved","identity":{"source_doc_id":"successful-zero"}}',
            ),
        )
        ledger = connection.execute(
            "SELECT * FROM source_coverage ORDER BY source,coverage_key"
        ).fetchall()
        documents = connection.execute("SELECT * FROM edinet_documents ORDER BY 1,2").fetchall()
    before_size = market.stat().st_size
    report = prepare_archive_execution(archives, market)
    assert report["cleared_archives"] == len(archives.archive_keys)
    assert market.stat().st_size < before_size
    with sqlite3.connect(market) as connection:
        assert connection.execute("PRAGMA freelist_count").fetchone() == (0,)
        assert (
            connection.execute(
                "SELECT * FROM source_coverage ORDER BY source,coverage_key"
            ).fetchall()
            == ledger
        )
        assert (
            connection.execute("SELECT * FROM edinet_documents ORDER BY 1,2").fetchall()
            == documents
        )
        assert read_scope(connection) == archives.scope
    assert store.get_keys == []


def test_failed_compaction_retries_without_rehydrating_cleared_zip(legacy, monkeypatch):
    store, _, archives, saved, market, mirror, _ = legacy
    pointer = store.values[POINTER]
    connect = sqlite3.connect
    compactions = []

    class RetriedCompaction(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "VACUUM":
                compactions.append(sql)
                if len(compactions) == 1:
                    raise sqlite3.OperationalError("disk full during compaction")
            return super().execute(sql, parameters)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: connect(*args, **kwargs, factory=RetriedCompaction),
    )
    with (
        pytest.raises(sqlite3.OperationalError, match="disk full"),
        publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, edinet_input=saved
        ),
    ):
        pytest.fail("failed compaction must stop before the SQL seal")
    assert archive_count(market) == 0
    with sqlite3.connect(market) as connection:
        assert connection.execute("PRAGMA freelist_count").fetchone()[0] > 0
        assert read_scope(connection) == archives.scope
    assert store.values[POINTER] == pointer
    assert store.put_keys == []
    before_size = market.stat().st_size
    with publication.prepare_market_lake(
        sqlite_path=market,
        mirror_root=mirror,
        store=store,
        release_id="retry-compaction",
        edinet_input=saved,
    ) as prepared:
        assert RAW not in prepared.export.datasets
        assert market.stat().st_size < before_size
        with sqlite3.connect(market) as connection:
            assert connection.execute("PRAGMA freelist_count").fetchone() == (0,)
    assert compactions == ["VACUUM", "VACUUM"]
    prepare_archive_execution(archives, market)
    assert compactions == ["VACUUM", "VACUUM"]
    assert store.values[POINTER] == pointer
    assert not any(key.endswith(".zip") for key in store.get_keys)
