"""TradingView stages need their originals and final same-day projection in the release."""

import json
from contextlib import closing
from datetime import timedelta

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.tradingview import (
    DAY,
    NOW,
    _sdk,
    native_inputs,
    payload,
    retain_result,
    seed_snapshot,
)
from tests.helpers.tradingview import store as tradingview_store

from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.receipts import available_handoffs
from baibai_jobs.integration import sources
from baibai_jobs.integration.tradingview_handoff import export_handoff, import_handoff
from baibai_jobs.publication import tradingview_verification as verification
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile
from baibai_jobs.publication.snapshot_release import publish_release

DATASETS = (
    "jquants.daily_bars",
    "tradingview.source_receipts",
    "tradingview.forecast_snapshots",
)


@pytest.fixture
def publication(tmp_path, monkeypatch):
    narrow_release_policy(monkeypatch, datasets=DATASETS, carries_history=None)
    remote = MemoryR2Store()
    database = tradingview_store(market_store(tmp_path / "market.sqlite"), 2)
    seed_snapshot(database, ["TSE:1001"], received=NOW - timedelta(minutes=1))

    def publish(source=database, *, identity="fixed"):
        mirror = tmp_path / identity
        with closing(open_connection(source)) as conn:
            present = [
                name
                for name in DATASETS
                if conn.execute(
                    f"SELECT COUNT(*) FROM {LAKE_DATASETS[name].sqlite_table}"
                ).fetchone()[0]
            ]
        with sealed_sqlite_snapshot(sqlite_path=source, mirror_root=mirror) as snapshot:
            manifests = [
                export_dataset_snapshot(
                    dataset=LAKE_DATASETS[name],
                    mirror_root=mirror,
                    source_snapshot=snapshot,
                    producer_git_commit="a" * 40,
                ).manifest_path
                for name in present
            ]
        manifest, _ = create_release(
            dataset_manifest_paths=manifests,
            mirror_root=mirror,
            release_id=identity,
            catalog=LAKE_DATASETS,
            policy=release_policy_for_profile("production"),
        )
        publish_release(mirror_root=mirror, release_manifest_path=manifest, store=remote)
        for key in ("market.sqlite", "runs.sqlite"):
            remote.store_object(
                key, source.read_bytes() if key == "market.sqlite" else b"saved runs"
            )
        remote.store_object(
            "machine-manifest.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "keys": {
                        key: remote.values[key].etag for key in ("market.sqlite", "runs.sqlite")
                    },
                }
            ).encode(),
        )
        return sources.canonical_generations(remote)

    return remote, database, publish


def new_stage(tmp_path, remote, database, *, result=None):
    raw = tradingview_store(tmp_path / "source.sqlite", 2)
    identity = retain_result(raw, result or _sdk(payload(["TSE:1000"])))
    packet = tmp_path / "packet"
    packet.mkdir()
    _, packet_master, packet_calendar = native_inputs(raw, tmp_path, day=DAY, remote=remote)
    handoff = export_handoff(
        raw,
        packet / "receipts.sqlite",
        day=DAY,
        master_input=packet_master,
        calendar_input=packet_calendar,
        since=NOW - timedelta(seconds=1),
    )
    (packet / "result.json").write_text(
        json.dumps(
            {
                "source": "tradingview",
                "asof": str(DAY),
                "started_at": NOW.isoformat(),
                **handoff.to_json(),
            }
        )
    )
    save_handoff(remote, "tradingview-new", packet, market=True)
    entry = available_handoffs(remote, market=True)[0]
    replay = import_handoff(packet / "receipts.sqlite", database, handoff, store=remote)
    return identity, entry, replay


def test_readback_requires_the_fixed_pointer_and_applied_original(tmp_path, publication):
    remote, database, publish = publication
    identity, _, _ = new_stage(tmp_path, remote, database)
    expected = verification.capture_publication(database, [DAY], receipt_ids=[identity])
    generations = publish()
    with pytest.raises(LakePublishError, match="fixed publication"):
        verification.verify_publication(
            remote,
            expected=expected,
            directory=tmp_path / "readback",
            generations={**generations, "lake_manifest_sha256": "f" * 64},
        )
    with pytest.raises(ValueError, match="not retained"):
        verification.capture_publication(database, [DAY], receipt_ids=["missing"])


def test_readback_does_not_fetch_or_compare_unrelated_history(tmp_path, publication):
    remote, database, publish = publication
    prior_day = DAY.replace(year=2025)
    old = retain_result(
        database,
        _sdk(payload(["TSE:1000"])),
        day=prior_day,
        master=prior_day,
        received=NOW.replace(year=2025),
    )
    identity, _, _ = new_stage(tmp_path, remote, database)
    expected = verification.capture_publication(database, [DAY], receipt_ids=[identity])
    with closing(open_connection(database)) as conn, conn:
        conn.execute(
            "UPDATE tradingview_source_receipts SET result_json='{}' WHERE receipt_id=?", (old,)
        )
    generations = publish()
    unrelated = {key for key in remote.values if key.endswith(".parquet") and "/year=2025/" in key}
    assert unrelated
    remote.get_keys.clear()
    assert verification.verify_publication(
        remote, expected=expected, directory=tmp_path / "readback", generations=generations
    ) == {"tradingview.source_receipts": 2, "tradingview.forecast_snapshots": 2}
    assert not unrelated.intersection(remote.get_keys)
