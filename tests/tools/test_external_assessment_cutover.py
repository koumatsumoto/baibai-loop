"""明示改名だけで保存済みの値・時刻・NULL・他tableを維持する。"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy
from tools.ops import migrate_market_30_to_31 as cutover

from baibai_engine.external_assessment.acquisition.verification import (
    history_digests,
    verify_release_history,
)
from baibai_engine.external_assessment.observations.models import (
    AnalystRatingObservation,
    ModelValuationObservation,
    QuantsScoreObservation,
)
from baibai_engine.external_assessment.query import external_assessment
from baibai_engine.external_assessment.reader import read_observations
from baibai_engine.external_assessment.store import save_observations
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import resolve_release
from baibai_engine.storage.lake.release import create_l1_release
from baibai_engine.storage.lake.writer import export_lake_legacy
from baibai_engine.storage.observations.schema import SQLiteSchemaError, open_connection

RECEIVED = datetime(2026, 10, 11, 15, 0, 0, 123456, tzinfo=UTC)


@pytest.fixture
def old_store(tmp_path):
    database = market_store(tmp_path / "old.sqlite")
    observations = []
    for received in (RECEIVED - timedelta(days=40), RECEIVED):
        common = dict(
            ticker="130A",
            fetched_at_utc=received,
            source_url="https://example.test/synthetic",
            parser_version="original-parser",
        )
        observations.extend(
            [
                ModelValuationObservation(
                    **common,
                    distributor="monex",
                    origin="ifis",
                    consensus_target=0,
                    consensus_target_count=0,
                    consensus_target_updated_label="元の更新表記",
                    per_company_forecast=None,
                ),
                AnalystRatingObservation(
                    **common,
                    distributor="rakuten",
                    origin="ifis",
                    rating=None,
                    rating_count=0,
                    rating_updated_label="元の表示",
                ),
                QuantsScoreObservation(
                    **common,
                    distributor="kabumap",
                    score_as_of_date=received.date(),
                    growth_score=0,
                    composite_score=80,
                ),
            ]
        )
    save_observations(database, observations)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO source_coverage(source,coverage_key,coverage_start,coverage_end,fetched_at_utc,record_count) VALUES(?,?,?,?,?,?)",
            ("synthetic-store-local", "all", "2026-10-01", "2026-10-12", "2026-10-12T12:00:00Z", 0),
        )
        connection.execute(
            "INSERT INTO lake_store_origin VALUES(1,?,?)", ("original-release", "a" * 64)
        )
        for old, new in cutover.TABLE_NAMES.items():
            connection.execute(f'ALTER TABLE "{new}" RENAME TO "{old}"')
            connection.execute(f'DROP INDEX "idx_{new}_snapshot"')
            connection.execute(f'CREATE INDEX "idx_{old}_snapshot" ON "{old}"(snapshot_date)')
        connection.execute("PRAGMA user_version=30")
    return database, observations


def test_migration_and_fixed_release_roundtrip_preserve_every_column_and_receipt(
    old_store, tmp_path, monkeypatch
):
    source, observations = old_store
    original_bytes = source.read_bytes()
    migrated = tmp_path / "migrated.sqlite"
    before = cutover.migrate_copy(source, migrated)
    assert source.read_bytes() == original_bytes
    assert all(before[old][0] == 2 for old in cutover.TABLE_NAMES)
    assert (
        read_observations(migrated, "130A", RECEIVED).model_valuations[0].latest == observations[3]
    )
    rating = read_observations(migrated, "130A", RECEIVED).analyst_ratings[0].latest
    assert rating == observations[4]
    assert rating.rating is None
    assert rating.rating_count == 0
    assessment = external_assessment(
        migrated, ticker="130A", cutoff=RECEIVED, evaluated_at=RECEIVED
    )
    assert assessment.analyst_ratings[0].comparison.reason == "latest_missing"
    assert save_observations(migrated, observations) == 0
    with pytest.raises(SQLiteSchemaError):
        read_observations(source, "130A", RECEIVED)
    datasets = (*cutover.DATASET_NAMES.values(), "jquants.daily_bars", "jquants.short_sale_reports")
    narrow_release_policy(
        monkeypatch, datasets=datasets, carries_history=True, minimum_population_count=None
    )
    mirror = tmp_path / "mirror"
    export = export_lake_legacy(
        sqlite_path=migrated,
        mirror_root=mirror,
        producer_git_commit="b" * 40,
        expected_store_origin=cutover.read_lake_store_origin(migrated),
    )
    manifest, release = create_l1_release(
        dataset_manifest_paths=[x.manifest_path for x in export.datasets.values()],
        mirror_root=mirror,
        release_id="new-names",
    )
    assert set(release.datasets) == set(datasets)
    fixed = resolve_release(
        LocalMirrorSource(mirror),
        release.release_id,
        manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    cache = LakeObjectCache(root=tmp_path / "empty-cache", source=LocalMirrorSource(mirror))
    restored = tmp_path / "restored.sqlite"
    open_connection(restored).close()
    with lake_session() as session:
        verify_release_history(
            session,
            release=fixed,
            cache=cache,
            expected_history=history_digests(migrated),
            observations=observations,
        )
        hydrate_market_store(
            session, release=fixed, cache=cache, store=restored, dataset_names=datasets
        )
    assert read_observations(restored, "130A", RECEIVED) == read_observations(
        migrated, "130A", RECEIVED
    )
    assert history_digests(restored) == history_digests(migrated)
    with sqlite3.connect(migrated) as connection:
        assert set(cutover.TABLE_NAMES).isdisjoint(cutover._tables(connection))
        assert cutover._digest(connection, "source_coverage") == before["source_coverage"]
        assert cutover._digest(connection, "lake_store_origin") == before["lake_store_origin"]
        indexes = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {f"idx_{name}_snapshot" for name in cutover.TABLE_NAMES.values()}.issubset(indexes)
        assert {f"idx_{name}_snapshot" for name in cutover.TABLE_NAMES}.isdisjoint(indexes)


def test_existing_destination_and_obsolete_source_are_never_overwritten(old_store, tmp_path):
    source, _ = old_store
    output = tmp_path / "existing.sqlite"
    output.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        cutover.migrate_copy(source, output)
    assert output.read_bytes() == b"keep"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA user_version=29")
    with pytest.raises(ValueError, match="schema 30"):
        cutover.migrate_copy(source, tmp_path / "refused.sqlite")
    assert not (tmp_path / "refused.sqlite").exists()


def test_bad_schema_cannot_leave_a_partially_migrated_copy(old_store, tmp_path):
    source, _ = old_store
    with sqlite3.connect(source) as connection:
        connection.execute("DROP TABLE jquants_daily_bars")
    before = source.read_bytes()
    output = tmp_path / "bad-copy.sqlite"
    with pytest.raises(SQLiteSchemaError):
        cutover.migrate_copy(source, output)
    assert not output.exists()
    assert source.read_bytes() == before


def _old_graph(source, mirror):
    """schema30の旧名contractで作る固定graph。新runtimeへ旧名aliasを登録しない。"""
    from dataclasses import replace

    from baibai_engine.storage.lake import writer
    from baibai_engine.storage.lake.datasets import LAKE_DATASETS
    from baibai_engine.storage.lake.keys import dataset_manifest_key, release_manifest_key
    from baibai_engine.storage.lake.models import (
        DatasetManifest,
        L1ReleaseSourceRef,
        ManifestTotals,
        ReleaseDataset,
        ReleaseManifest,
        SQLiteSnapshotSourceRef,
        canonical_lake_model_bytes,
    )

    snapshot_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    snapshot = SQLiteSnapshotSourceRef(
        source_id=f"market-v30-{snapshot_hash[:24]}",
        sha256=snapshot_hash,
        kind="sqlite_snapshot",
        schema_version=30,
        captured_at=RECEIVED,
    )
    names = {
        **cutover.DATASET_NAMES,
        "jquants.daily_bars": "jquants.daily_bars",
        "jquants.short_sale_reports": "jquants.short_sale_reports",
    }
    entries = {}
    with sqlite3.connect(source) as connection:
        for index, (old_name, new_name) in enumerate(names.items()):
            current = LAKE_DATASETS[new_name]
            old_table = next(
                (a for a, b in cutover.TABLE_NAMES.items() if b == current.sqlite_table),
                current.sqlite_table,
            )
            dataset = replace(current, name=old_name, sqlite_table=old_table)
            periods = writer._selected_periods(connection, dataset)
            staging = mirror / f"synthetic-staging-{index}"
            staging.mkdir(parents=True)
            built = [
                writer._build_period(
                    dataset=dataset,
                    period=period,
                    staging_root=staging,
                    rows=writer._period_rows(connection, dataset, period),
                    sources=(snapshot,),
                )
                for period in periods
            ]
            writer._promote_partitions(mirror, built)
            partitions = tuple(x.manifest for x in built)
            manifest = DatasetManifest(
                manifest_version=1,
                dataset=old_name,
                layer="l1_canonical",
                contract_version=1,
                build_id=f"old-build-{index}",
                sources=(),
                producer_git_commit="a" * 40,
                created_at=RECEIVED,
                coverage_start=date.fromisoformat(
                    connection.execute(
                        f"SELECT MIN({dataset.date_column}) FROM {dataset.sqlite_table}"
                    ).fetchone()[0]
                ),
                data_as_of=writer._data_as_of(connection, dataset, periods=periods),
                population_count=None,
                coverage_status="partial",
                partition_by=dataset.partition_by,
                partitions=partitions,
                totals=ManifestTotals(
                    objects=len(built),
                    bytes=sum(x.bytes for p in partitions for x in p.objects),
                    rows=sum(x.rows for p in partitions for x in p.objects),
                ),
            )
            payload = canonical_lake_model_bytes(manifest)
            path = mirror / dataset_manifest_key(dataset=old_name, build_id=manifest.build_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            entries[old_name] = ReleaseDataset(
                build_id=manifest.build_id,
                contract_version=1,
                manifest_sha256=hashlib.sha256(payload).hexdigest(),
                data_as_of=manifest.data_as_of,
                coverage_status=manifest.coverage_status,
                totals=manifest.totals,
            )
    release = ReleaseManifest(
        manifest_version=1,
        release_id="old-release",
        profile="production",
        created_at=RECEIVED,
        data_as_of=min(x.data_as_of for x in entries.values()),
        datasets=entries,
    )
    payload = canonical_lake_model_bytes(release)
    key = release_manifest_key(release_id=release.release_id)
    path = mirror / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    ref = L1ReleaseSourceRef(
        kind="l1_release",
        source_id=release.release_id,
        key=key,
        sha256=hashlib.sha256(payload).hexdigest(),
        manifest_version=1,
    )
    reference = mirror / "old-ref.json"
    reference.write_text(ref.model_dump_json())
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE lake_store_origin SET release_id=?, release_manifest_sha256=?",
            (ref.source_id, ref.sha256),
        )
    return reference


def test_offline_preparation_maps_history_floors_without_pointer_or_old_graph_changes(
    old_store, tmp_path, monkeypatch
):
    source, observations = old_store
    mirror = tmp_path / "old-and-new-mirror"
    reference = _old_graph(source, mirror)
    old_files = {p.relative_to(mirror): p.read_bytes() for p in mirror.rglob("*") if p.is_file()}
    migrated = tmp_path / "migrated.sqlite"
    cutover.migrate_copy(source, migrated)
    narrow_release_policy(
        monkeypatch,
        datasets=(
            *cutover.DATASET_NAMES.values(),
            "jquants.daily_bars",
            "jquants.short_sale_reports",
        ),
        minimum_population_count=None,
        carries_history=True,
    )
    monkeypatch.setattr(cutover, "verified_git_commit", lambda: "b" * 40)
    manifest = cutover.prepare_release(migrated, mirror, reference)
    import json

    identity = json.loads(manifest.read_bytes())
    fixed = resolve_release(
        LocalMirrorSource(mirror),
        identity["release_id"],
        manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    assert set(cutover.DATASET_NAMES).isdisjoint(fixed.dataset_manifests)
    with lake_session() as session:
        verify_release_history(
            session,
            release=fixed,
            cache=LakeObjectCache(root=tmp_path / "fresh", source=LocalMirrorSource(mirror)),
            expected_history=history_digests(migrated),
            observations=observations,
        )
    for name, payload in old_files.items():
        assert (mirror / name).read_bytes() == payload
    assert not (mirror / "lake/pointers/l1/current.json").exists()
    with sqlite3.connect(migrated) as connection:
        connection.execute("UPDATE lake_store_origin SET release_id='other-release'")
    with pytest.raises(ValueError, match="not bound"):
        cutover.prepare_release(migrated, mirror, reference)
