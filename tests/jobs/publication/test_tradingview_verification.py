"""TradingView stages need their originals and final same-day projection in the release."""

import json
import shutil
from contextlib import closing
from datetime import timedelta

import pytest
from mcp.types import CallToolResult, TextContent
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.master_calendar_native import seed_calendar, seed_master
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.tradingview import DAY, NOW, _sdk, payload, retain_result, seed_snapshot
from tests.helpers.tradingview import store as tradingview_store

from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.economic_staging import save_handoff, stage_prefix
from baibai_jobs.acquisition.receipts import available_handoffs
from baibai_jobs.integration import sources
from baibai_jobs.integration.tradingview_handoff import export_handoff, import_handoff
from baibai_jobs.publication import tradingview_verification as verification
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.lease import _load_handle, _save_handle, acquire, release
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
    handoff = export_handoff(
        raw, packet / "receipts.sqlite", day=DAY, since=NOW - timedelta(seconds=1)
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
    replay = import_handoff(packet / "receipts.sqlite", database, handoff)
    return identity, entry, replay


def seal_command(tmp_path, monkeypatch, remote, entry, replay, *, expected=None):
    work = tmp_path / "integration"
    work.mkdir()
    report = {
        "handoffs": [
            {
                "entry": entry,
                "result": replay.report,
                "applied": True,
                "recovery": not replay.complete,
            }
        ]
    }
    if expected is not None:
        report["tradingview_publication"] = expected
    (work / "applied.json").write_text(json.dumps(report))
    handle, _ = acquire(remote, "operator")
    lease = tmp_path / "lease.json"
    _save_handle(lease, handle)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: remote)
    return ["seal", "--directory", str(work), "--lease-handle", str(lease)], work


@pytest.mark.parametrize("capture", [False, True])
def test_unpublished_tradingview_cannot_be_sealed_against_a_coherent_old_bundle(
    tmp_path, monkeypatch, publication, capture
):
    remote, database, publish = publication
    publish(identity="old")
    remote_before = remote.values["market.sqlite"]
    identity, entry, replay = new_stage(tmp_path, remote, database)
    expected = (
        verification.capture_publication(database, [DAY], receipt_ids=[identity])
        if capture
        else None
    )
    command, _ = seal_command(tmp_path, monkeypatch, remote, entry, replay, expected=expected)
    remote.put_keys.clear()
    assert sources.main(command) == 1
    assert remote.put_keys == []
    assert remote.values["market.sqlite"] == remote_before
    assert available_handoffs(remote, market=True) == [entry]


def test_successful_readback_seals_and_retry_preserves_the_exact_marker(
    tmp_path, monkeypatch, publication
):
    remote, database, publish = publication
    identity, entry, replay = new_stage(tmp_path, remote, database)
    expected = verification.capture_publication(database, [DAY], receipt_ids=[identity])
    command, work = seal_command(tmp_path, monkeypatch, remote, entry, replay, expected=expected)
    generations = publish()
    assert sources.main(command) == 0
    key = stage_prefix(entry["stage_id"], market=True) + "/integrated.json"
    marker = remote.values[key]
    assert json.loads(marker.body)["canonical_generations"] == generations
    assert json.loads((work / "applied.json").read_text())["tradingview_readback"] == {
        "tradingview.source_receipts": 2,
        "tradingview.forecast_snapshots": 2,
    }
    assert available_handoffs(remote, market=True) == []
    remote.put_keys.clear()
    assert sources.main(command) == 0
    assert remote.put_keys == []
    assert remote.values[key] == marker


@pytest.mark.parametrize(
    "changed",
    ["body", "projection", "missing_original", "missing_prior_original", "missing_projection"],
)
def test_changed_originals_or_projection_cannot_consume_the_stage(
    tmp_path, monkeypatch, publication, changed
):
    remote, database, publish = publication
    identity, entry, replay = new_stage(tmp_path, remote, database)
    expected = verification.capture_publication(database, [DAY], receipt_ids=[identity])
    command, _ = seal_command(tmp_path, monkeypatch, remote, entry, replay, expected=expected)
    wrong = tmp_path / "wrong.sqlite"
    shutil.copyfile(database, wrong)
    with closing(open_connection(wrong)) as conn, conn:
        if changed == "body":
            conn.execute(
                "UPDATE tradingview_source_receipts SET result_json=? WHERE receipt_id=?",
                ('{"different":"same-count original"}', identity),
            )
        elif changed == "projection":
            conn.execute(
                "UPDATE tradingview_forecast_snapshots SET quote_close=999 WHERE ticker='1000'"
            )
        elif changed == "missing_original":
            conn.execute("DELETE FROM tradingview_source_receipts WHERE receipt_id=?", (identity,))
        elif changed == "missing_prior_original":
            conn.execute("DELETE FROM tradingview_source_receipts WHERE receipt_id!=?", (identity,))
        else:
            conn.execute("DELETE FROM tradingview_forecast_snapshots WHERE ticker='1000'")
    publish(wrong)
    remote.put_keys.clear()
    assert sources.main(command) == 1
    assert remote.put_keys == []
    assert available_handoffs(remote, market=True) == [entry]


@pytest.mark.parametrize("keep_prior", [False, True])
def test_rejected_response_reads_back_originals_before_sealing_explicit_failure(
    tmp_path, monkeypatch, publication, keep_prior
):
    remote, database, publish = publication
    if not keep_prior:
        with closing(open_connection(database)) as conn, conn:
            conn.execute("DELETE FROM tradingview_source_receipts")
            conn.execute("DELETE FROM tradingview_forecast_snapshots")
    identity, entry, replay = new_stage(
        tmp_path,
        remote,
        database,
        result=CallToolResult(isError=True, content=[TextContent(type="text", text="HTTP 429")]),
    )
    assert not replay.complete
    assert replay.report["receipt_ids"] == [identity]
    expected = verification.capture_publication(database, [DAY], receipt_ids=[identity])
    command, work = seal_command(tmp_path, monkeypatch, remote, entry, replay, expected=expected)
    publish()
    assert sources.main(command) == 0
    marker = stage_prefix(entry["stage_id"], market=True) + "/integrated.json"
    assert json.loads(remote.values[marker].body)["status"] == "needs_explicit_recovery"
    assert json.loads((work / "applied.json").read_text())["tradingview_readback"] == {
        "tradingview.source_receipts": 1 + int(keep_prior),
        "tradingview.forecast_snapshots": int(keep_prior),
    }


def test_apply_freezes_the_final_same_day_original_and_projection_closure(
    tmp_path, monkeypatch, publication
):
    remote, database, _ = publication
    identity, entry, replay = new_stage(tmp_path, remote, database)
    saved_edinet = empty_edinet_input(remote, tmp_path)
    seed_calendar(remote, database, tmp_path / "calendar", as_of=DAY)
    seed_master(remote, database, tmp_path / "master", as_of=DAY)
    work = tmp_path / "apply"
    work.mkdir()
    (work / "plan.json").write_text(
        json.dumps({"handoffs": [entry], "edinet_input": saved_edinet.model_dump(mode="json")})
    )
    monkeypatch.setattr(sources, "_replay", lambda *args, **kwargs: replay)
    result = sources.apply(remote, work, market=database, asof=DAY)
    assert result["tradingview_publication"] == verification.capture_publication(
        database, [DAY], receipt_ids=[identity]
    )
    assert result["tradingview_publication"]["tradingview.source_receipts"]["rows"] == 2


def test_lost_lease_after_readback_stops_before_the_first_tradingview_marker(
    tmp_path, monkeypatch, publication
):
    remote, database, publish = publication
    identity, entry, replay = new_stage(tmp_path, remote, database)
    expected = verification.capture_publication(database, [DAY], receipt_ids=[identity])
    command, _ = seal_command(tmp_path, monkeypatch, remote, entry, replay, expected=expected)
    publish()
    verify = sources.verify_tradingview_publication

    def release_after_readback(*args, **kwargs):
        result = verify(*args, **kwargs)
        release(remote, _load_handle(tmp_path / "lease.json"))
        return result

    monkeypatch.setattr(sources, "verify_tradingview_publication", release_after_readback)
    remote.put_keys.clear()
    assert sources.main(command) == 1
    assert not any(key.endswith("/integrated.json") for key in remote.put_keys)
    assert available_handoffs(remote, market=True) == [entry]


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
