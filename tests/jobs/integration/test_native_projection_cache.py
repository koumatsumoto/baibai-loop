"""A projection cache proves normalization as well as content and SQL parity."""

import hashlib

import pyarrow.parquet as pq
import pytest
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.observations.jquants_decode import (
    JQUANTS_NUMERIC_PROJECTION_REVISION,
)
from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import (
    JQUANTS_DAILY_BARS,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.snapshots.contracts import PROJECTION_REVISION_METADATA_KEY
from baibai_engine.storage.snapshots.keys import canonical_object_key
from baibai_engine.storage.snapshots.models import DatasetManifest, NativeL1SourceRef
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_jobs.integration import (
    margin_alerts_native,
    margin_interest_native,
    short_sale_reports_native,
)
from baibai_jobs.integration import native_projection_cache as cache
from baibai_jobs.publication import (
    l1_margin_alerts_export,
    l1_margin_interest_export,
    l1_short_sale_reports_export,
)


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
        ((b"jquants-numeric-v2", b"jquants-numeric-v2"), False),
        ((b"jquants-numeric-v3", b"jquants-numeric-v3"), False),
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


@pytest.mark.parametrize(
    "handler",
    [margin_alerts_native, margin_interest_native, short_sale_reports_native],
    ids=["margin-alerts", "margin-interest", "short-sale-reports"],
)
@pytest.mark.parametrize(
    "revision_marker",
    [{"projection_revision": "jquants-numeric-v2"}, {}],
    ids=["older-revision", "before-revision-was-recorded"],
)
def test_rebuild_cli_reports_old_projection_revision_before_reading_originals(
    tmp_path, monkeypatch, capsys, handler, revision_marker
):
    day = "2026-11-02"
    descriptor = {
        "format": handler.INPUT_FORMAT,
        "head_sha256": "a" * 64,
        "selection_sha256": "b" * 64,
        "as_of": day,
        "summary": {
            "selection_start": day,
            "selection_end": day,
            "evaluated_at": "2026-11-03T00:00:00Z",
            # All three model_input writers set this on derived coverage before
            # they started recording the projection revision.
            "coverage_status": "complete",
            **revision_marker,
        },
    }
    digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    saved = InputSnapshot(
        kind="native_l1", reference="fixture-" + digest, sha256=digest, descriptor=descriptor
    )
    path = tmp_path / "previous-input.json"
    path.write_text(saved.model_dump_json(), encoding="utf-8")
    output = tmp_path / "out"
    store = MemoryR2Store()
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    monkeypatch.setattr(handler, "Boto3R2Store", lambda **_kwargs: store)

    assert (
        handler.main(
            [
                "--saved-input",
                str(path),
                "--work-directory",
                str(tmp_path / "unused-work"),
                "--start",
                day,
                "--end",
                day,
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert "use the recorded producer commit" in capsys.readouterr().err
    assert not output.exists() or not tuple(output.iterdir())
    assert store.get_keys == store.put_keys == store.head_keys == []


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


@pytest.mark.parametrize(
    ("handler", "writer", "export"),
    [
        (
            margin_alerts_native,
            l1_margin_alerts_export,
            l1_margin_alerts_export.export_margin_alerts_from_native_input,
        ),
        (
            margin_interest_native,
            l1_margin_interest_export,
            l1_margin_interest_export.export_margin_interest_from_native_input,
        ),
        (
            short_sale_reports_native,
            l1_short_sale_reports_export,
            l1_short_sale_reports_export.export_short_sale_reports_from_native_input,
        ),
    ],
    ids=["margin-alerts", "margin-interest", "short-sale-reports"],
)
@pytest.mark.parametrize(
    "revision_marker", [{}, {"projection_revision": "jquants-numeric-v2"}], ids=["absent", "older"]
)
def test_current_writers_refuse_an_unproven_projection_before_export(
    tmp_path, monkeypatch, handler, writer, export, revision_marker
):
    day = "2026-11-02"
    descriptor = {
        "format": handler.INPUT_FORMAT,
        "selection_sha256": "b" * 64,
        "summary": {
            "selection_start": day,
            "selection_end": day,
            "coverage_status": "complete",
            "weekly_coverage_status": "complete",
            "daily_coverage_status": "complete",
            **revision_marker,
        },
    }
    digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    saved = InputSnapshot(
        kind="native_l1", reference="fixture-" + digest, sha256=digest, descriptor=descriptor
    )

    def forbidden(*args, **kwargs):
        pytest.fail("unproven projection reached the Parquet writer")

    monkeypatch.setattr(writer, "export_dataset_snapshot", forbidden)
    with pytest.raises(cache.SavedProjectionRevisionMismatch, match="projection revision"):
        export(
            source_snapshot=None,
            saved=saved,
            mirror_root=tmp_path,
            producer_git_commit="a" * 40,
        )
    assert not any(tmp_path.iterdir())


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
