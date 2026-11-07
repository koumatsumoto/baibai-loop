"""有限移行は読取cutoffで原本を隠さず、全partitionから境界違反を報告する。"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.jquants_receipts import body, receive
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.snapshot_contracts import JQUANTS_SOURCE_PAGES
from baibai_engine.storage.snapshots.keys import release_manifest_key
from baibai_engine.storage.snapshots.models import (
    ReleaseDataset,
    ReleaseManifest,
    canonical_lake_model_bytes,
)
from baibai_engine.storage.snapshots.objects import open_lake
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_jobs.maintenance import (
    margin_alerts_native_cutover,
    margin_interest_native_cutover,
)

AT = datetime(2026, 11, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    ("source", "cutover"),
    [
        ("margin_alerts", margin_alerts_native_cutover),
        ("margin_interest", margin_interest_native_cutover),
    ],
)
@pytest.mark.parametrize(
    "future",
    [
        datetime(2026, 11, 7, tzinfo=UTC),
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    ],
)
def test_future_receipt_blocks_migration_across_month_partitions(tmp_path, source, cutover, future):
    db, mirror = tmp_path / "legacy.sqlite", tmp_path / "mirror"
    for received in (AT, future):
        day = (received.date() - timedelta(days=1)).isoformat()
        query = {"from": day, "to": day} if source == "market_calendar" else {"date": day}
        receive(db, source, query, body([]), received)
    with sealed_sqlite_snapshot(sqlite_path=db, mirror_root=mirror) as snapshot:
        report = export_dataset_snapshot(
            dataset=JQUANTS_SOURCE_PAGES,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            source_snapshot=snapshot,
            build_id="originals",
            created_at=future,
        )
    dataset = report.manifest
    release = ReleaseManifest(
        manifest_version=2,
        release_id="synthetic",
        profile="production",
        created_at=future,
        data_as_of=dataset.data_as_of,
        datasets={
            dataset.dataset: ReleaseDataset(
                build_id=dataset.build_id,
                layer=dataset.layer,
                contract_version=dataset.contract_version,
                manifest_sha256=hashlib.sha256(report.manifest_path.read_bytes()).hexdigest(),
                data_as_of=dataset.data_as_of,
                coverage_status=dataset.coverage_status,
                totals=dataset.totals,
            )
        },
    )
    payload = canonical_lake_model_bytes(release)
    path = mirror / release_manifest_key(release_id=release.release_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    store = MemoryR2Store()
    with open_lake(mirror=mirror) as (session, cache):
        fixed = resolve_release(
            cache.source, release.release_id, manifest_sha256=hashlib.sha256(payload).hexdigest()
        )
        plan = cutover.prepare_migration(
            session,
            release=fixed,
            cache=cache,
            store=store,
            known_at=AT + timedelta(days=2),
            directory=tmp_path / "plan",
        )
    assert len(plan["attempts"]) == 1
    assert len(plan["blocked"]) == 1
    assert plan["blocked"][0]["reason"] == "receipt is later than the fixed migration boundary"
    assert not store.put_keys
