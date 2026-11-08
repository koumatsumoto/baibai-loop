"""Keep public response identities while changing only physical Parquet grouping."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.master_calendar_native import (
    native_master_calendar_publication as native_master_calendar_publication,
)
from tests.helpers.price_native import native_price_publication as native_price_publication
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.public_pages.models import EquityWebSourceResponse
from baibai_engine.data_collection.receipts.public_pages.snapshot_contracts import (
    EQUITY_WEB_SOURCE_RESPONSES as DATASET,
)
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import hydrate_market_store
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import (
    iter_partition_rows,
    materialize_partitions,
    resolve_release,
)
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import (
    LakeBuildError,
    export_market_store,
    validate_snapshot_parity,
)
from baibai_engine.storage.sqlite.source_cache.equity_web_import import import_responses
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    advance_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication import snapshot_publication as publication
from baibai_jobs.publication.policy import release_policy_for_profile
from baibai_jobs.publication.public_page_verification import (
    history_digests,
    response_digest,
    verify_release_history,
)
from baibai_jobs.publication.snapshot_release import publish_release

AT = datetime(2026, 10, 1, tzinfo=UTC)


def response(number, day, *, body=b"original", parent=None):
    return EquityWebSourceResponse(
        receipt_id=str(UUID(int=number)),
        attempt_id=str(UUID(int=number + 1000)) if parent is None else parent.attempt_id,
        source="rakuten",
        requested_ticker="7203",
        role="page" if parent is None else "iframe",
        parent_receipt_id=None if parent is None else parent.receipt_id,
        requested_url="https://example.test/synthetic",
        received_at_utc=AT + timedelta(days=day),
        selected_encoding="utf-8",
        body=body,
    )


def build(database, mirror, identity, *, dataset=DATASET, previous=None):
    report = export_market_store(
        catalog={DATASET.name: dataset},
        sqlite_path=database,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        expected_store_origin=None,
        created_at=AT + timedelta(days=40),
        previous=previous.dataset_manifests if previous is not None else None,
    )
    path, _ = create_release(
        dataset_manifest_paths=[report.datasets[DATASET.name].manifest_path],
        mirror_root=mirror,
        release_id=identity,
        created_at=AT + timedelta(days=40),
        catalog={DATASET.name: DATASET},
        policy=release_policy_for_profile("production"),
    )
    fixed = resolve_release(
        LocalMirrorSource(mirror),
        identity,
        manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    return path, fixed


def read_rows(mirror, fixed):
    paths = materialize_partitions(
        LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
        dataset=DATASET,
        partitions=fixed.dataset_manifest(DATASET.name).partitions,
    )
    with lake_session() as session:
        return [
            row
            for batch in iter_partition_rows(session, dataset=DATASET, paths=paths)
            for row in batch
        ]


@pytest.fixture(autouse=True)
def _policy(monkeypatch):
    narrow_release_policy(monkeypatch, datasets=(DATASET.name,))


def test_old_month_and_new_days_keep_receipts_parent_clock_body_and_hydration(tmp_path):
    database, mirror = tmp_path / "market.sqlite", tmp_path / "mirror"
    open_connection(database).close()
    parent = replace(
        response(90, 30, body=b"\x00non-UTF8\xff"),
        received_at_utc=AT + timedelta(days=30, hours=23, minutes=59),
    )
    child = response(10, 31, parent=parent, body=b"")
    originals = (child, response(50, 2, body=b"malformed but received"), parent)
    import_responses(database, originals)
    source_bytes = database.read_bytes()
    _, old = build(database, mirror, "old-month", dataset=replace(DATASET, object_date_grain=None))
    _, new = build(database, mirror, "new-days")
    old_manifest, new_manifest = (
        old.dataset_manifest(DATASET.name),
        new.dataset_manifest(DATASET.name),
    )
    assert old_manifest.totals.objects == 2
    assert new_manifest.totals.objects == 3
    assert old_manifest.partition_by == new_manifest.partition_by == ("year", "month")
    assert old_manifest.contract_version == new_manifest.contract_version == 1
    assert old_manifest.coverage_status == new_manifest.coverage_status == "partial"
    expected = read_rows(mirror, old)
    assert read_rows(mirror, new) == expected
    assert database.read_bytes() == source_bytes
    assert [row[0] for row in expected] == sorted(item.receipt_id for item in originals)
    expected_history = history_digests(database, months={(2026, 10), (2026, 11)})
    cache = LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror))
    for fixed in (old, new):
        with lake_session() as session:
            verify_release_history(
                session,
                release=fixed,
                cache=cache,
                expected_history=expected_history,
                selected_digests={item.receipt_id: response_digest(item) for item in originals},
            )
            clean = tmp_path / f"{fixed.release_id}.sqlite"
            open_connection(clean).close()
            hydrate_market_store(
                session,
                catalog={DATASET.name: DATASET},
                release=fixed,
                cache=cache,
                store=clean,
                dataset_names=(DATASET.name,),
            )
        with closing(sqlite3.connect(clean)) as conn:
            assert (
                conn.execute(
                    "SELECT * FROM equity_web_source_responses ORDER BY receipt_id"
                ).fetchall()
                == expected
            )
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_next_day_and_late_receipts_only_rewrite_their_day_and_keep_old_input(tmp_path):
    database, mirror = tmp_path / "market.sqlite", tmp_path / "mirror"
    open_connection(database).close()
    import_responses(database, (response(90, 0), response(10, 1), response(50, 2)))
    store = MemoryR2Store()
    path, initial = build(database, mirror, "initial")
    publish_release(mirror_root=mirror, release_manifest_path=path, store=store)
    original_rows = read_rows(mirror, initial)
    original_bytes = {
        key: value.body
        for key, value in store.values.items()
        if not key.startswith("lake/pointers/")
    }
    for index, added in enumerate((None, response(40, 3), response(30, 1)), start=1):
        if added is not None:
            import_responses(database, (added,))
        path, fixed = build(database, mirror, f"later-{index}")
        store.put_keys.clear()
        publish_release(mirror_root=mirror, release_manifest_path=path, store=store)
        parquet_puts = [key for key in store.put_keys if key.endswith(".parquet")]
        assert len(parquet_puts) == (0 if added is None else 1)
        assert read_rows(mirror, initial) == original_rows
        assert all(store.values[key].body == body for key, body in original_bytes.items())
        with closing(sqlite3.connect(database)) as conn:
            assert (
                read_rows(mirror, fixed)
                == conn.execute(
                    "SELECT * FROM equity_web_source_responses ORDER BY receipt_id"
                ).fetchall()
            )


def test_parity_accepts_shuffled_daily_objects_and_rejects_missing_or_corrupt_bytes(tmp_path):
    database, mirror = tmp_path / "market.sqlite", tmp_path / "mirror"
    open_connection(database).close()
    import_responses(database, (response(90, 0), response(10, 1), response(50, 2)))
    _, fixed = build(database, mirror, "days")
    manifest = fixed.dataset_manifest(DATASET.name)
    part = manifest.partitions[0]
    shuffled = manifest.model_copy(
        update={"partitions": (part.model_copy(update={"objects": tuple(reversed(part.objects))}),)}
    )
    validate_snapshot_parity(
        dataset=DATASET, sqlite_path=database, mirror_root=mirror, manifest=shuffled
    )
    missing = manifest.model_copy(
        update={"partitions": (part.model_copy(update={"objects": part.objects[:-1]}),)}
    )
    with pytest.raises(LakeBuildError, match="omit SQLite partition rows"):
        validate_snapshot_parity(
            dataset=DATASET, sqlite_path=database, mirror_root=mirror, manifest=missing
        )
    path = mirror / part.objects[0].key
    path.write_bytes(path.read_bytes() + b"corruption")
    with pytest.raises(LakeBuildError, match="checksum or size mismatch"):
        validate_snapshot_parity(
            dataset=DATASET, sqlite_path=database, mirror_root=mirror, manifest=manifest
        )


def test_publication_keeps_exact_old_months_and_splits_only_changed_periods(tmp_path, monkeypatch):
    database, mirror = tmp_path / "market.sqlite", tmp_path / "legacy"
    open_connection(database).close()
    import_responses(
        database,
        (response(90, 0), response(10, 1), response(80, 31), response(20, 32)),
    )
    path, original = build(
        database, mirror, "legacy-months", dataset=replace(DATASET, object_date_grain=None)
    )
    store = MemoryR2Store()
    publish_release(mirror_root=mirror, release_manifest_path=path, store=store)
    original_rows = read_rows(mirror, original)
    old_objects = {
        obj.key: store.values[obj.key].body
        for part in original.dataset_manifest(DATASET.name).partitions
        for obj in part.objects
    }
    advance_lake_store_origin(
        database,
        expected=None,
        target=LakeStoreOrigin(original.release_id, hashlib.sha256(path.read_bytes()).hexdigest()),
    )
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    create = publication.create_lake_release
    monkeypatch.setattr(
        publication,
        "create_lake_release",
        lambda **kwargs: create(created_at=AT + timedelta(days=41), **kwargs),
    )
    # A cold mirror proves that prior body files are not the source of these rows.
    for index, (added, expected_puts, sizes) in enumerate(
        (
            (None, 0, (1, 1)),
            (response(30, 33), 3, (1, 3)),
            (response(40, 34), 1, (1, 4)),
            (response(50, 1), 2, (2, 4)),
        )
    ):
        if added is not None:
            import_responses(database, (added,))
        current_mirror = tmp_path / f"fresh-{index}"
        store.put_keys.clear()
        with publication.prepare_market_lake(
            sqlite_path=database,
            mirror_root=current_mirror,
            store=store,
            release_id=f"retained-{index}",
        ) as prepared:
            manifest = prepared.export.datasets[DATASET.name].manifest
            assert tuple(len(part.objects) for part in manifest.partitions) == sizes
            if index < 3:
                assert (
                    manifest.partitions[0].objects
                    == original.dataset_manifest(DATASET.name).partitions[0].objects
                )
            assert all(
                part.sources == (prepared.source_snapshot.ref,) for part in manifest.partitions
            )
            publication.publish_prepared_market_lake(prepared, store=store)
            current = resolve_release(
                LocalMirrorSource(current_mirror),
                prepared.release.release_id,
                manifest_sha256=prepared.release_sha256,
            )
        assert len([key for key in store.put_keys if key.endswith(".parquet")]) == expected_puts
        with closing(sqlite3.connect(database)) as conn:
            assert (
                read_rows(current_mirror, current)
                == conn.execute(
                    "SELECT * FROM equity_web_source_responses ORDER BY receipt_id"
                ).fetchall()
            )
        assert read_rows(mirror, original) == original_rows
        assert all(store.values[key].body == body for key, body in old_objects.items())


pytestmark = pytest.mark.usefixtures(
    "native_price_publication", "native_master_calendar_publication"
)
