"""Historical bytes keep their contract; newer observations roundtrip independently."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.machine_analysis.observations.financials.snapshot_contracts import (
    EDINET_METRICS as CURRENT,
)
from baibai_engine.machine_analysis.observations.financials.snapshot_contracts import (
    EDINET_METRICS_V2,
    EDINET_METRICS_V3,
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
    accepted_dataset,
    resolve_current_release,
    resolve_release,
)
from baibai_engine.storage.snapshots.release import LakeReleasePointer, create_release
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication.policy import release_policy_for_profile

NOW = datetime(2026, 11, 16, tzinfo=UTC)


@pytest.fixture(params=[(EDINET_METRICS_V2, 51), (EDINET_METRICS_V3, 52)], ids=["v2", "v3"])
def old_release(tmp_path, monkeypatch, request):
    legacy, version = request.param
    narrow_release_policy(monkeypatch, datasets=(CURRENT.name,))
    source = tmp_path / f"v{legacy.contract_version}.sqlite"
    with sqlite3.connect(source) as c:
        columns = ",".join(
            f"{column.name} {column.sqlite_type}" + ("" if column.nullable else " NOT NULL")
            for column in legacy.columns
        )
        c.execute(f"CREATE TABLE edinet_metrics({columns}, PRIMARY KEY(asof_date,ticker))")
        c.execute(f"PRAGMA user_version={version}")
        c.executemany(
            "INSERT INTO edinet_metrics(asof_date,ticker,fcf_ttm) VALUES (?,?,?)",
            [("2026-10-01", "0000", 70), ("2026-11-16", "0000", 80)],
        )
        if legacy.contract_version == 3:
            c.execute(
                "UPDATE edinet_metrics SET financing_lease_repayments_cash_effect=-12, "
                "financing_noncontrolling_dividends_cash_effect=-3 WHERE asof_date='2026-11-16'"
            )
    mirror = tmp_path / "mirror"
    snapshot = writer.SealedSQLiteSnapshot(
        path=source,
        sealed_at=NOW,
        ref=SQLiteSnapshotSourceRef(
            kind="sqlite_snapshot",
            source_id=f"market-v{version}-" + "a" * 24,
            sha256="a" * 64,
            schema_version=version,
            captured_at=NOW,
        ),
    )
    with monkeypatch.context() as old:
        old.setattr(writer, "SQLITE_SCHEMA_VERSION", version)
        exported = writer.export_dataset_snapshot(
            dataset=legacy,
            source_snapshot=snapshot,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            created_at=NOW,
            build_id="old-edinet",
        )
    policy = release_policy_for_profile("production")
    path, release = create_release(
        catalog={legacy.name: legacy},
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
    return legacy, cache, fixed, exported, mirror, policy


def test_legacy_hydrates_without_fabricating_observations_and_new_publication_is_v4(
    old_release, tmp_path
):
    legacy, cache, fixed, old, mirror, policy = old_release
    assert accepted_dataset(fixed, CURRENT) == legacy
    with pytest.raises(ValueError, match="accepts only v4"):
        create_release(
            catalog={CURRENT.name: CURRENT},
            policy=policy,
            dataset_manifest_paths=[old.manifest_path],
            mirror_root=mirror,
            created_at=NOW,
            release_id="must-not-publish-old-schema",
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
    query = (
        "SELECT asof_date,fcf_ttm,financing_lease_repayments_cash_effect,"
        "financing_noncontrolling_dividends_cash_effect,parent_attributable_equity_json "
        "FROM edinet_metrics ORDER BY asof_date"
    )
    financing = (-12, -3) if legacy.contract_version == 3 else (None, None)
    previous_rows = [
        ("2026-10-01", 70, None, None, None),
        ("2026-11-16", 80, *financing, None),
    ]
    serialized_fact = json.dumps(
        {
            "amount_yen": 2**53 + 1,
            "source_context": "InterimInstant_EquityAttributableToOwnersOfParentIFRSMember",
            "source_element": "jpigp_cor:EquityIFRS",
            "source_csv_member": "XBRL_TO_CSV/facts.csv",
            "archive_sha256": "a" * 64,
            "observation_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(out) as c:
        assert c.execute(query).fetchall() == previous_rows
        c.execute(
            "INSERT INTO edinet_metrics (asof_date,ticker,source_doc_id,document_type,"
            "consolidation_basis,parent_attributable_equity_json,source_document_revision) "
            "VALUES (?,?,?,?,?,?,?)",
            ("2026-11-17", "0000", "S100TEST", "160", "consolidated", serialized_fact, "1" * 64),
        )
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
    assert new.manifest.contract_version == 4
    assert new.manifest.totals.rows == 3
    old_keys = {obj.key for part in old.manifest.partitions for obj in part.objects}
    new_keys = {obj.key for part in new.manifest.partitions for obj in part.objects}
    assert not old_keys.intersection(new_keys)
    assert all((mirror / key).is_file() for key in old_keys | new_keys)
    assert (
        accepted_dataset(
            resolve_current_release(cache.source, catalog={CURRENT.name: CURRENT}, policy=policy),
            CURRENT,
        )
        == legacy
    )
    new_path, new_release = create_release(
        catalog={CURRENT.name: CURRENT},
        policy=policy,
        dataset_manifest_paths=[new.manifest_path],
        mirror_root=mirror,
        created_at=NOW,
        release_id="new-edinet-release",
    )
    fixed_new = resolve_release(
        cache.source,
        new_release.release_id,
        manifest_sha256=hashlib.sha256(new_path.read_bytes()).hexdigest(),
    )
    hydrated = tmp_path / "v4.sqlite"
    with closing(open_connection(hydrated)):
        pass
    with lake_session() as session:
        hydrate_market_store(
            session,
            catalog={CURRENT.name: CURRENT},
            release=fixed_new,
            cache=cache,
            store=hydrated,
            dataset_names=(CURRENT.name,),
        )
    with sqlite3.connect(hydrated) as c:
        assert c.execute(query).fetchall() == [
            *previous_rows,
            ("2026-11-17", None, None, None, serialized_fact),
        ]
