"""旧固定quantsは元契約で検証し、現行storeへ明示的に投影する。"""

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pyarrow.parquet as pq
import pytest
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.machine_analysis.observations.external_assessments.contract import (
    QUANTS_SCORE_SCHEMA50_COLUMNS,
    QUANTS_SCORE_TABLE,
    _ddl,
)
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    QuantsScoreObservation,
)
from baibai_engine.machine_analysis.observations.external_assessments.reader import (
    read_observations,
)
from baibai_engine.machine_analysis.observations.external_assessments.rows import observation_row
from baibai_engine.machine_analysis.observations.external_assessments.snapshot_contracts import (
    EXTERNAL_ASSESSMENT_QUANTS_SCORE_OBSERVATIONS as CURRENT,
)
from baibai_engine.machine_analysis.observations.external_assessments.snapshot_contracts import (
    EXTERNAL_ASSESSMENT_QUANTS_SCORE_OBSERVATIONS_V1 as LEGACY,
)
from baibai_engine.storage.snapshots import writer
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import hydrate_market_store
from baibai_engine.storage.snapshots.keys import current_release_pointer_key
from baibai_engine.storage.snapshots.models import (
    SQLiteSnapshotSourceRef,
    canonical_lake_model_bytes,
)
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import (
    LakeReadError,
    accepted_dataset,
    materialize_partitions,
    resolve_current_release,
    verify_object,
)
from baibai_engine.storage.snapshots.release import LakeReleasePointer, create_release
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication.external_assessment_verification import (
    history_digests,
    verify_release_history,
)
from baibai_jobs.publication.policy import release_policy_for_profile

NOW = datetime(2026, 10, 10, tzinfo=UTC)


@pytest.fixture
def historical_lake(tmp_path, monkeypatch):
    narrow_release_policy(monkeypatch, datasets=(CURRENT.name,))
    source = tmp_path / "old.sqlite"
    observation = QuantsScoreObservation(
        ticker="660A",
        fetched_at_utc=NOW,
        source_url="https://example.invalid/synthetic",
        parser_version="kabumap-quants-3",
        score_as_of_date=date(2026, 10, 9),
        growth_score=7,
    )
    value = observation.model_dump(mode="json")
    value["fetched_at_utc"] = NOW.isoformat(timespec="microseconds")
    names = tuple(item[0] for item in QUANTS_SCORE_SCHEMA50_COLUMNS)
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.executescript(_ddl(QUANTS_SCORE_TABLE, QUANTS_SCORE_SCHEMA50_COLUMNS))
        connection.execute("PRAGMA user_version=50")
        connection.execute(
            f"INSERT INTO {QUANTS_SCORE_TABLE} VALUES ({','.join('?' for _ in names)})",
            tuple(value[name] for name in names),
        )
        older = {
            **value,
            "fetched_at_utc": (NOW - timedelta(days=30)).isoformat(timespec="microseconds"),
            "snapshot_date": "2026-09-10",
            "score_as_of_date": "2026-09-09",
            "growth_score": 6,
        }
        connection.execute(
            f"INSERT INTO {QUANTS_SCORE_TABLE} VALUES ({','.join('?' for _ in names)})",
            tuple(older[name] for name in names),
        )
    mirror = tmp_path / "mirror"
    snapshot = writer.SealedSQLiteSnapshot(
        path=source,
        sealed_at=NOW,
        ref=SQLiteSnapshotSourceRef(
            kind="sqlite_snapshot",
            source_id=f"market-v50-{'a' * 24}",
            sha256="a" * 64,
            schema_version=50,
            captured_at=NOW,
        ),
    )
    # Build authentic v1 objects with the old writer's schema expectation, locally only.
    with monkeypatch.context() as old:
        old.setattr(writer, "SQLITE_SCHEMA_VERSION", 50)
        report = writer.export_dataset_snapshot(
            dataset=LEGACY,
            source_snapshot=snapshot,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            created_at=NOW,
            build_id="legacy-quants",
        )
    policy = release_policy_for_profile("production")
    manifest_path, release = create_release(
        catalog={LEGACY.name: LEGACY},
        policy=policy,
        dataset_manifest_paths=[report.manifest_path],
        mirror_root=mirror,
        created_at=NOW,
        release_id="legacy-release",
    )
    pointer = LakeReleasePointer(
        release_id=release.release_id,
        manifest_key=manifest_path.relative_to(mirror).as_posix(),
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    pointer_path = mirror / current_release_pointer_key()
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(canonical_lake_model_bytes(pointer))
    cache = LakeObjectCache(root=tmp_path / "cache", source=LocalMirrorSource(mirror))
    fixed = resolve_current_release(cache.source, catalog={CURRENT.name: CURRENT}, policy=policy)
    return cache, fixed, report, observation, mirror, policy


def test_v1_fixed_read_is_exact_and_new_publication_rejects_v1(historical_lake):
    cache, release, report, _, mirror, policy = historical_lake
    selected = accepted_dataset(release, CURRENT)
    assert selected == LEGACY
    paths = materialize_partitions(cache, dataset=selected, partitions=report.manifest.partitions)
    assert pq.read_schema(paths[0]).equals(
        LEGACY.arrow_schema_for("legacy_sqlite_import"), check_metadata=True
    )
    with pytest.raises(LakeReadError, match="schema does not match"):
        verify_object(paths[0], CURRENT, report.manifest.partitions[0].objects[0])
    with pytest.raises(ValueError, match="accepts only v2"):
        create_release(
            catalog={CURRENT.name: CURRENT},
            policy=policy,
            dataset_manifest_paths=[report.manifest_path],
            mirror_root=mirror,
            created_at=NOW,
            release_id="must-not-publish-v1",
        )
    with pytest.raises(ValueError, match="no declared read contract"):
        CURRENT.read_contract(3)
    with pytest.raises(ValueError, match="row source differs"):
        CURRENT.project_read_row(replace(LEGACY, columns=CURRENT.columns), ())


def test_v1_hydrates_current_shape_and_reexports_all_rows_as_v2(historical_lake, tmp_path):
    cache, release, old_report, expected, mirror, _ = historical_lake
    destination = tmp_path / "current.sqlite"
    with closing(open_connection(destination)):
        pass
    with lake_session() as session:
        hydrate_market_store(
            session,
            catalog={CURRENT.name: CURRENT},
            release=release,
            cache=cache,
            store=destination,
            dataset_names=(CURRENT.name,),
        )
    with lake_session() as session:
        verify_release_history(
            session,
            release=release,
            cache=cache,
            expected_history=history_digests(destination),
            observations=(expected,),
        )
    history = read_observations(destination, "660A", NOW)
    assert history.quants_scores[0].latest == expected
    assert history.quants_scores[0].latest.availability == "available"
    missing = QuantsScoreObservation(
        ticker="660A",
        fetched_at_utc=NOW + timedelta(days=1),
        source_url="https://example.invalid/synthetic",
        parser_version="kabumap-quants-4",
        availability="not_computed",
        score_as_of_date=None,
    )
    table, names, values = observation_row(missing)
    with closing(open_connection(destination)) as connection, connection:
        connection.execute(
            f"INSERT INTO {table}({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
            values,
        )
    with writer.sealed_sqlite_snapshot(sqlite_path=destination, mirror_root=mirror) as snapshot:
        exported = writer.export_dataset_snapshot(
            dataset=CURRENT,
            source_snapshot=snapshot,
            mirror_root=mirror,
            producer_git_commit="b" * 40,
            created_at=NOW,
            build_id="current-quants",
            previous=old_report.manifest,
        )
    assert exported.manifest.contract_version == 2
    assert exported.manifest.totals.rows == 3
    assert old_report.manifest.totals.rows == 2
    assert {tuple(sorted(part.values.items())) for part in exported.manifest.partitions} == {
        tuple(sorted(part.values.items())) for part in old_report.manifest.partitions
    }
    old_keys = {obj.key for part in old_report.manifest.partitions for obj in part.objects}
    assert not old_keys.intersection(
        obj.key for part in exported.manifest.partitions for obj in part.objects
    )
    materialize_partitions(cache, dataset=CURRENT, partitions=exported.manifest.partitions)
    from baibai_engine.storage.snapshots.hydrate import load_dataset_rows

    restored = tmp_path / "restored.sqlite"
    with closing(open_connection(restored)) as connection, connection, lake_session() as session:
        assert (
            load_dataset_rows(
                connection,
                session=session,
                cache=cache,
                dataset=CURRENT,
                partitions=exported.manifest.partitions,
            )
            == 3
        )
    pair = read_observations(restored, "660A", NOW + timedelta(days=1)).quants_scores[0]
    assert pair.latest == missing
    assert pair.previous == expected
