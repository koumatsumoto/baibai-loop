"""Historical v2 bytes keep their contract; current hydration adds only NULL details."""

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.machine_analysis.observations.financials.snapshot_contracts import (
    EDINET_METRICS as CURRENT,
)
from baibai_engine.machine_analysis.observations.financials.snapshot_contracts import (
    EDINET_METRICS_V2 as LEGACY,
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
from baibai_engine.storage.snapshots.reader import accepted_dataset, resolve_current_release
from baibai_engine.storage.snapshots.release import LakeReleasePointer, create_release
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication.policy import release_policy_for_profile

NOW = datetime(2026, 11, 16, tzinfo=UTC)


@pytest.fixture
def old_release(tmp_path, monkeypatch):
    narrow_release_policy(monkeypatch, datasets=(CURRENT.name,))
    source = tmp_path / "v2.sqlite"
    with sqlite3.connect(source) as c:
        columns = ",".join(
            f"{column.name} {column.sqlite_type}" + ("" if column.nullable else " NOT NULL")
            for column in LEGACY.columns
        )
        c.execute(f"CREATE TABLE edinet_metrics({columns}, PRIMARY KEY(asof_date,ticker))")
        c.execute("PRAGMA user_version=51")
        c.executemany(
            "INSERT INTO edinet_metrics(asof_date,ticker,fcf_ttm) VALUES (?,?,?)",
            [("2026-10-01", "0000", 70), ("2026-11-16", "0000", 80)],
        )
    mirror = tmp_path / "mirror"
    snapshot = writer.SealedSQLiteSnapshot(
        path=source,
        sealed_at=NOW,
        ref=SQLiteSnapshotSourceRef(
            kind="sqlite_snapshot",
            source_id="market-v51-" + "a" * 24,
            sha256="a" * 64,
            schema_version=51,
            captured_at=NOW,
        ),
    )
    with monkeypatch.context() as old:
        old.setattr(writer, "SQLITE_SCHEMA_VERSION", 51)
        exported = writer.export_dataset_snapshot(
            dataset=LEGACY,
            source_snapshot=snapshot,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            created_at=NOW,
            build_id="old-edinet",
        )
    policy = release_policy_for_profile("production")
    path, release = create_release(
        catalog={LEGACY.name: LEGACY},
        policy=policy,
        dataset_manifest_paths=[exported.manifest_path],
        mirror_root=mirror,
        created_at=NOW,
        release_id="old-edinet-release",
    )
    pointer = LakeReleasePointer(
        release_id=release.release_id,
        manifest_key=path.relative_to(mirror).as_posix(),
        manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    pointer_path = mirror / current_release_pointer_key()
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(canonical_lake_model_bytes(pointer))
    cache = LakeObjectCache(root=tmp_path / "cache", source=LocalMirrorSource(mirror))
    fixed = resolve_current_release(cache.source, catalog={CURRENT.name: CURRENT}, policy=policy)
    return cache, fixed, exported, mirror, policy


def test_v2_hydrates_nulls_and_new_publication_is_v3(old_release, tmp_path):
    cache, fixed, old, mirror, policy = old_release
    assert accepted_dataset(fixed, CURRENT) == LEGACY
    with pytest.raises(ValueError, match="accepts only v3"):
        create_release(
            catalog={CURRENT.name: CURRENT},
            policy=policy,
            dataset_manifest_paths=[old.manifest_path],
            mirror_root=mirror,
            created_at=NOW,
            release_id="must-not-publish-v2",
        )
    out = tmp_path / "current.sqlite"
    with closing(open_connection(out)):
        pass
    with lake_session() as session:
        hydrate_market_store(
            session,
            catalog={CURRENT.name: CURRENT},
            release=fixed,
            cache=cache,
            store=out,
            dataset_names=(CURRENT.name,),
        )
    with sqlite3.connect(out) as c:
        assert c.execute(
            "SELECT asof_date,fcf_ttm,financing_lease_repayments_cash_effect,financing_noncontrolling_dividends_cash_effect FROM edinet_metrics ORDER BY asof_date"
        ).fetchall() == [("2026-10-01", 70, None, None), ("2026-11-16", 80, None, None)]
    with writer.sealed_sqlite_snapshot(sqlite_path=out, mirror_root=mirror) as snapshot:
        new = writer.export_dataset_snapshot(
            dataset=CURRENT,
            source_snapshot=snapshot,
            mirror_root=mirror,
            producer_git_commit="b" * 40,
            created_at=NOW,
            build_id="new-edinet",
            previous=old.manifest,
        )
    assert new.manifest.contract_version == 3
    assert new.manifest.totals.rows == 2
    old_keys = {obj.key for part in old.manifest.partitions for obj in part.objects}
    new_keys = {obj.key for part in new.manifest.partitions for obj in part.objects}
    assert not old_keys.intersection(new_keys)
    assert all((mirror / key).is_file() for key in old_keys | new_keys)
    assert (
        accepted_dataset(
            resolve_current_release(cache.source, catalog={CURRENT.name: CURRENT}, policy=policy),
            CURRENT,
        )
        == LEGACY
    )
