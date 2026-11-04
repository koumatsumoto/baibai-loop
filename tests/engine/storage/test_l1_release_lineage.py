from __future__ import annotations

import gzip
import hashlib
import json
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store

from baibai_engine.storage.snapshots.keys import dataset_manifest_key
from baibai_engine.storage.snapshots.models import (
    DatasetManifest,
    L1DatasetSourceRef,
    canonical_lake_model_bytes,
)
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile


def test_release_accepts_exact_l1_lineage_and_rejects_wrong_or_reverse_source(
    tmp_path, monkeypatch
):
    names = ("jquants.source_pages", "jquants.daily_bars")
    narrow_release_policy(monkeypatch, datasets=names)
    source = market_store(tmp_path / "source.sqlite")
    with closing(open_connection(source)) as conn, conn:
        conn.execute(
            "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "r",
                0,
                "daily_bars",
                json.dumps({"date": "20260105"}),
                "2026-02-01T00:00:00+00:00",
                "2026-02-01",
                gzip.compress(b'{"data":[]}', mtime=0),
            ),
        )
    mirror = tmp_path / "mirror"
    when = datetime(2026, 2, 4, tzinfo=UTC)
    with sealed_sqlite_snapshot(sqlite_path=source, mirror_root=mirror) as snapshot:
        l1, l2 = [
            export_dataset_snapshot(
                dataset=LAKE_DATASETS[name],
                mirror_root=mirror,
                source_snapshot=snapshot,
                producer_git_commit="a" * 40,
                build_id=f"source-{index}",
                created_at=when,
            )
            for index, name in enumerate(names)
        ]
    l1_sha = hashlib.sha256(l1.manifest_path.read_bytes()).hexdigest()
    ref = L1DatasetSourceRef(
        kind="l1_dataset",
        source_id=f"l1-{l1_sha[:24]}",
        sha256=l1_sha,
        dataset=names[0],
        build_id=l1.manifest.build_id,
        key=dataset_manifest_key(dataset=names[0], build_id=l1.manifest.build_id),
    )
    payload = l2.manifest.model_dump(mode="json")
    for partition in payload["partitions"]:
        partition["sources"] = [ref.model_dump(mode="json")]
    derived = DatasetManifest.model_validate_json(json.dumps(payload))
    candidate = mirror / "derived.json"
    candidate.write_bytes(canonical_lake_model_bytes(derived))
    _, release = create_release(
        dataset_manifest_paths=[l1.manifest_path, candidate],
        mirror_root=mirror,
        release_id="direct-l1",
        created_at=when,
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    assert set(release.datasets) == set(names)
    bad_sha = "b" * 64
    wrong = ref.model_copy(update={"sha256": bad_sha, "source_id": f"l1-{bad_sha[:24]}"})
    for partition in payload["partitions"]:
        partition["sources"] = [wrong.model_dump(mode="json")]
    candidate.write_bytes(
        canonical_lake_model_bytes(DatasetManifest.model_validate_json(json.dumps(payload)))
    )
    with pytest.raises(ValueError, match="exact L1"):
        create_release(
            dataset_manifest_paths=[l1.manifest_path, candidate],
            mirror_root=mirror,
            release_id="wrong-l1",
            created_at=when,
            catalog=LAKE_DATASETS,
            policy=release_policy_for_profile("production"),
        )
    # A reference cannot label a derived L2 dataset as an L1 source.
    l2_sha = hashlib.sha256(l2.manifest_path.read_bytes()).hexdigest()
    wrong_layer = L1DatasetSourceRef(
        kind="l1_dataset",
        source_id=f"l1-{l2_sha[:24]}",
        sha256=l2_sha,
        dataset=names[1],
        build_id=l2.manifest.build_id,
        key=dataset_manifest_key(dataset=names[1], build_id=l2.manifest.build_id),
    )
    for partition in payload["partitions"]:
        partition["sources"] = [wrong_layer.model_dump(mode="json")]
    candidate.write_bytes(
        canonical_lake_model_bytes(DatasetManifest.model_validate_json(json.dumps(payload)))
    )
    with pytest.raises(ValueError, match="exact L1"):
        create_release(
            dataset_manifest_paths=[l1.manifest_path, candidate],
            mirror_root=mirror,
            release_id="l2-as-l1",
            created_at=when,
            catalog=LAKE_DATASETS,
            policy=release_policy_for_profile("production"),
        )
    reverse = l1.manifest.model_dump(mode="json")
    for partition in reverse["partitions"]:
        partition["sources"] = [ref.model_dump(mode="json")]
    with pytest.raises(ValueError, match="L1 source partitions"):
        DatasetManifest.model_validate_json(json.dumps(reverse))


def test_derived_parquet_schema_is_distinct_and_validated(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import (
        JQUANTS_DAILY_BARS,
    )
    from baibai_engine.storage.snapshots.models import LakeObject
    from baibai_engine.storage.snapshots.reader import LakeReadError, verify_object

    dataset = JQUANTS_DAILY_BARS
    row = {column.name: None for column in dataset.columns}
    row.update({"ticker": "1301", "traded_at": "2026-01-05"})
    path = tmp_path / "derived.parquet"
    pq.write_table(pa.Table.from_pylist([row], schema=dataset.arrow_schema_for("l1_derived")), path)
    data = path.read_bytes()
    item = LakeObject(
        key="lake/l2/test.parquet",
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        rows=1,
        min_key=("1301", "2026-01-05"),
        max_key=("1301", "2026-01-05"),
    )
    verify_object(path, dataset, item, source_kind="l1_derived")
    with pytest.raises(LakeReadError, match="schema"):
        verify_object(path, dataset, item)


def test_l1_only_duckdb_export_publishes_and_reads_without_old_l2(tmp_path, monkeypatch):
    import duckdb

    from baibai_engine.data_collection.providers.jquants.lake_receipts import iter_retained_requests
    from baibai_engine.machine_analysis.observations.prices.from_l1 import (
        materialize_price_days,
        rebuild_price_days,
    )
    from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
    from baibai_engine.storage.snapshots.reader import materialize_partitions, resolve_release
    from baibai_engine.storage.snapshots.writer import export_l1_derived_dataset

    names = ("jquants.source_pages", "jquants.daily_bars")
    narrow_release_policy(monkeypatch, datasets=names)
    source = market_store(tmp_path / "source.sqlite")
    # The previous L2 contains other data; this new raw receipt is the sole input.
    with closing(open_connection(source)) as conn, conn:
        body = json.dumps({"data": [{"Code": "13010", "Date": "2026-01-05", "C": 123}]}).encode()
        conn.execute(
            "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "native",
                0,
                "daily_bars",
                json.dumps({"date": "20260105"}),
                "2026-02-01T00:00:00+00:00",
                "2026-02-01",
                gzip.compress(body, mtime=0),
            ),
        )
    mirror = tmp_path / "mirror"
    when = datetime(2026, 2, 4, tzinfo=UTC)
    with sealed_sqlite_snapshot(sqlite_path=source, mirror_root=mirror) as snapshot:
        l1 = export_dataset_snapshot(
            dataset=LAKE_DATASETS[names[0]],
            mirror_root=mirror,
            source_snapshot=snapshot,
            producer_git_commit="a" * 40,
            build_id="native-input",
            created_at=when,
        )
    source.unlink()
    l1_sha = hashlib.sha256(l1.manifest_path.read_bytes()).hexdigest()
    objects = [mirror / obj.key for part in l1.manifest.partitions for obj in part.objects]
    with duckdb.connect(":memory:") as conn:
        requests = iter_retained_requests(conn, objects, source="daily_bars", known_at=when)
        days = rebuild_price_days(requests, days=[date(2026, 1, 5)])
        materialize_price_days(conn, days.values(), l1_coverage_status="partial")
        l2 = export_l1_derived_dataset(
            dataset=LAKE_DATASETS[names[1]],
            source_manifest=l1.manifest,
            source_manifest_sha256=l1_sha,
            connection=conn,
            table="daily_bars",
            coverage_status="complete",
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            build_id="derived-output",
            created_at=when,
        )
    release_path, release = create_release(
        dataset_manifest_paths=[l1.manifest_path, l2.manifest_path],
        mirror_root=mirror,
        release_id="native-only",
        created_at=when,
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    from tests.helpers.r2_store import MemoryR2Store

    from baibai_jobs.publication.snapshot_release import publish_release

    remote = MemoryR2Store()
    published = publish_release(
        mirror_root=mirror, release_manifest_path=release_path, store=remote
    )
    assert published.release_id == "native-only"
    resolved = resolve_release(
        LocalMirrorSource(mirror),
        release_id=release.release_id,
        manifest_sha256=hashlib.sha256(release_path.read_bytes()).hexdigest(),
    )
    paths = materialize_partitions(
        LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
        dataset=LAKE_DATASETS[names[1]],
        partitions=resolved.dataset_manifest(names[1]).partitions,
    )
    with duckdb.connect(":memory:") as conn:
        assert conn.execute(
            "SELECT ticker, close FROM read_parquet(?)", [[str(p) for p in paths]]
        ).fetchall() == [("1301", 123.0)]
