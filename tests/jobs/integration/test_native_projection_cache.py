"""A projection cache proves normalization as well as content and SQL parity."""

import hashlib

import pyarrow.parquet as pq
import pytest
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.observations.jquants_decode import (
    JQUANTS_NUMERIC_PROJECTION_REVISION,
)
from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import (
    JQUANTS_DAILY_BARS,
)
from baibai_engine.storage.snapshots.contracts import PROJECTION_REVISION_METADATA_KEY
from baibai_engine.storage.snapshots.keys import canonical_object_key
from baibai_engine.storage.snapshots.models import DatasetManifest, NativeL1SourceRef
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_jobs.integration import native_projection_cache as cache


def projection(tmp_path, revisions):
    market = market_store(
        tmp_path / "market.sqlite",
        daily_bars=(("1301", "2026-01-05", 100.0, 1000.0), ("1301", "2026-01-20", 105.0, 1100.0)),
    )
    mirror = tmp_path / "mirror"
    store = MemoryR2Store()
    source = NativeL1SourceRef(
        kind="native_l1",
        source_id="native-l1-" + "a" * 24,
        sha256="a" * 64,
        namespace="example",
        key="fixed-inputs/example/v1/selections/" + "a" * 64 + ".json",
    )
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as snapshot:
        report = export_dataset_snapshot(
            dataset=JQUANTS_DAILY_BARS,
            source_snapshot=snapshot,
            native_source=source,
            native_coverage_status="complete",
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            projection_revision=JQUANTS_NUMERIC_PROJECTION_REVISION,
            object_date_grain="day",
        )
    assert len(report.manifest.partitions) == 1
    part = report.manifest.partitions[0]
    assert len(part.objects) == len(revisions) == 2
    objects = []
    for obj, revision in zip(part.objects, revisions, strict=True):
        path = mirror / obj.key
        parquet = pq.ParquetFile(path)
        table, schema = parquet.read(), parquet.schema_arrow
        temporary = tmp_path / "replacement.parquet"
        with pq.ParquetWriter(temporary, schema) as writer:
            if revision is not None:
                writer.add_key_value_metadata({PROJECTION_REVISION_METADATA_KEY: revision})
            writer.write_table(table)
        body = temporary.read_bytes() if revision != b"corrupt" else b"not parquet"
        digest = hashlib.sha256(body).hexdigest()
        key = canonical_object_key(
            layer=JQUANTS_DAILY_BARS.layer,
            dataset=JQUANTS_DAILY_BARS.name,
            contract_version=JQUANTS_DAILY_BARS.contract_version,
            partition_values=part.values,
            partition_by=JQUANTS_DAILY_BARS.partition_by,
            content_sha256=digest,
        )
        replacement = mirror / key
        replacement.parent.mkdir(parents=True, exist_ok=True)
        replacement.write_bytes(body)
        store.store_object(key, body, sha256=digest)
        updated = obj.model_copy(update={"key": key, "sha256": digest, "bytes": len(body)})
        objects.append(updated)
    updated = report.manifest.model_copy(
        update={
            "partitions": (part.model_copy(update={"objects": tuple(objects)}),),
            "totals": report.manifest.totals.model_copy(
                update={"bytes": sum(obj.bytes for obj in objects)}
            ),
        }
    )
    return store, market, mirror, DatasetManifest.model_validate_json(updated.model_dump_json())


def cached(store, market, mirror, manifest, read_scopes):
    return cache.verified_cached_scopes(
        store,
        manifest,
        market=market,
        mirror_root=mirror,
        dataset=JQUANTS_DAILY_BARS,
        namespace="example",
        input_format="example-input-v1",
        read_scopes=read_scopes,
    )


_CURRENT = JQUANTS_NUMERIC_PROJECTION_REVISION.encode()


@pytest.mark.parametrize(
    ("revisions", "accepted"),
    [
        ((_CURRENT, _CURRENT), True),
        ((None, None), False),
        ((b"jquants-numeric-v1", b"jquants-numeric-v1"), False),
        ((_CURRENT, None), False),
        ((_CURRENT, b"jquants-numeric-v1"), False),
        ((_CURRENT, b"corrupt"), False),
    ],
)
def test_every_verified_object_requires_current_footer(tmp_path, revisions, accepted):
    store, market, mirror, manifest = projection(tmp_path, revisions)
    calls = []

    def read_scopes(descriptor):
        calls.append(descriptor)
        return {"saved-day": {"adopted": "saved-receipt"}}

    result = cached(store, market, mirror, manifest, read_scopes)
    assert bool(result) == accepted
    assert bool(calls) == accepted
    assert store.get_keys == []


def test_sha_is_proved_before_footer_is_read(tmp_path, monkeypatch):
    store, market, mirror, manifest = projection(tmp_path, (_CURRENT, _CURRENT))
    obj = manifest.partitions[0].objects[0]
    (mirror / obj.key).write_bytes(b"damaged local")
    store.store_object(obj.key, b"damaged remote", sha256=obj.sha256)

    def forbidden(*args, **kwargs):
        pytest.fail("unverified body reached footer or source reader")

    monkeypatch.setattr(cache.pq, "read_metadata", forbidden)
    assert cached(store, market, mirror, manifest, forbidden) == {}
    assert store.get_keys == [obj.key]


def test_no_objects_is_not_vacuous_normalization_proof(tmp_path):
    store, market, mirror, manifest = projection(tmp_path, (_CURRENT, _CURRENT))
    empty = manifest.model_copy(
        update={
            "partitions": tuple(
                part.model_copy(update={"objects": ()}) for part in manifest.partitions
            )
        }
    )

    def forbidden(*args, **kwargs):
        pytest.fail("empty object set reached source reader")

    assert cached(store, market, mirror, empty, forbidden) == {}
