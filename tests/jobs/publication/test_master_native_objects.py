"""Year/v3 readers keep compatibility while native master objects move by snapshot date."""

from datetime import timedelta

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.price_native import native_price_publication as native_price_publication
from tests.jobs.integration.test_master_native_models import AT, DAY, attempt, records, setup_native

from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import materialize_partitions
from baibai_engine.storage.snapshots.writer import LakeBuildError, validate_snapshot_parity
from baibai_jobs.publication.datasets import LAKE_DATASETS


def test_actual_publication_reuses_prior_days_and_year_v3_multiobject_reader(tmp_path, monkeypatch):
    import pyarrow.parquet as pq

    import baibai_jobs.publication.snapshot_publication as publication

    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    narrow_release_policy(monkeypatch, datasets=("jquants.master_snapshots",))
    monkeypatch.setattr(
        publication,
        "LAKE_DATASETS",
        {"jquants.master_snapshots": LAKE_DATASETS["jquants.master_snapshots"]},
    )
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    older = DAY.replace(year=DAY.year - 1)
    store, owner, market = setup_native(tmp_path, [attempt("old", day=older), attempt("current")])
    mirror = tmp_path / "mirror"
    added, manifests = [], []
    cycles = [
        None,
        attempt("equal", at=AT + timedelta(minutes=1)),
        attempt("new-day", day=DAY + timedelta(days=1), at=AT + timedelta(minutes=2)),
    ]
    corrected = records(DAY)
    corrected[0]["CoName"] = "訂正された名称"
    cycles.append(attempt("correction", at=AT + timedelta(minutes=3), rows=corrected))
    for number, received in enumerate(cycles):
        if received:
            owner.commit([received])
        store.put_keys.clear()
        with publication.prepare_market_lake(
            sqlite_path=market,
            mirror_root=mirror,
            store=store,
            as_of=DAY + timedelta(days=1),
            release_id=f"master-cycle-{number}",
        ) as prepared:
            manifest = prepared.export.datasets["jquants.master_snapshots"].manifest
            manifests.append(manifest)
            assert manifest.contract_version == 3
            assert manifest.partition_by == ("year",)
            publication.publish_prepared_market_lake(prepared, store=store)
        added.append(
            len(
                [
                    key
                    for key in store.put_keys
                    if "/jquants.master_snapshots/" in key and key.endswith(".parquet")
                ]
            )
        )
    assert added == [2, 0, 1, 1]
    initial = {obj.min_key[0]: obj.key for part in manifests[0].partitions for obj in part.objects}
    final = {obj.min_key[0]: obj.key for part in manifests[-1].partitions for obj in part.objects}
    assert initial[older.isoformat()] == final[older.isoformat()]
    assert initial[DAY.isoformat()] != final[DAY.isoformat()]
    assert all(
        obj.rows == 2503 and obj.min_key[0] == obj.max_key[0]
        for part in manifests[-1].partitions
        for obj in part.objects
    )
    assert set(initial.values()) <= set(store.values)  # No remote deletion or GC assumption.
    paths = materialize_partitions(
        LakeObjectCache(root=tmp_path / "cold", source=LocalMirrorSource(mirror)),
        dataset=LAKE_DATASETS["jquants.master_snapshots"],
        partitions=manifests[-1].partitions,
    )
    assert sum(len(pq.read_table(path)) for path in paths) == 7509
    with publication.sealed_sqlite_snapshot(
        sqlite_path=market, mirror_root=tmp_path / "parity"
    ) as sealed:
        validate_snapshot_parity(
            dataset=LAKE_DATASETS["jquants.master_snapshots"],
            sqlite_path=sealed.path,
            mirror_root=mirror,
            manifest=manifests[-1],
        )
        broken = manifests[-1].model_copy(
            update={
                "partitions": tuple(
                    part.model_copy(update={"objects": part.objects[:1]})
                    for part in manifests[-1].partitions
                )
            }
        )
        with pytest.raises(LakeBuildError, match="omit SQLite"):
            validate_snapshot_parity(
                dataset=LAKE_DATASETS["jquants.master_snapshots"],
                sqlite_path=sealed.path,
                mirror_root=mirror,
                manifest=broken,
            )


pytestmark = pytest.mark.usefixtures("native_price_publication")
