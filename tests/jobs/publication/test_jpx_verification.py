"""JPX sealing requires exact fixed-release originals and replaced projection scopes."""

import json
import shutil
from contextlib import closing
from datetime import timedelta

import pytest
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.master_calendar_native import seed_calendar, seed_master
from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.integration.test_jpx_handoff import DAY, STAMP, earnings, regulations

from baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild import rebuild_jpx_snapshot
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.economic_staging import save_handoff, stage_prefix
from baibai_jobs.acquisition.receipts import available_handoffs, seal_integrated
from baibai_jobs.integration import sources
from baibai_jobs.integration.jpx_handoff import export_handoff, import_handoff
from baibai_jobs.publication import jpx_verification as verification
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.lease import _save_handle, acquire, release
from baibai_jobs.publication.policy import release_policy_for_profile
from baibai_jobs.publication.snapshot_release import publish_release

DATASETS = ("jquants.daily_bars", *verification._DATASETS)


@pytest.fixture
def publication(tmp_path, monkeypatch):
    narrow_release_policy(monkeypatch, datasets=DATASETS, carries_history=None)
    store = MemoryR2Store()
    database = market_store(tmp_path / "market.sqlite")
    old = earnings(database, STAMP - timedelta(hours=1), "2026-10-18", "7203")
    rebuild_jpx_snapshot(database, old)
    regulation = regulations(database, DAY, STAMP)
    rebuild_jpx_snapshot(database, regulation)

    def publish(source=database, *, identity="fixed"):
        mirror = tmp_path / identity
        with sealed_sqlite_snapshot(sqlite_path=source, mirror_root=mirror) as snapshot:
            manifests = [
                export_dataset_snapshot(
                    dataset=LAKE_DATASETS[name],
                    mirror_root=mirror,
                    source_snapshot=snapshot,
                    producer_git_commit="a" * 40,
                ).manifest_path
                for name in DATASETS
            ]
        manifest, _ = create_release(
            dataset_manifest_paths=manifests,
            mirror_root=mirror,
            release_id=identity,
            catalog=LAKE_DATASETS,
            policy=release_policy_for_profile("production"),
        )
        publish_release(mirror_root=mirror, release_manifest_path=manifest, store=store)
        for key in ("market.sqlite", "runs.sqlite"):
            store.store_object(
                key, source.read_bytes() if key == "market.sqlite" else b"saved runs"
            )
        store.store_object(
            "machine-manifest.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "keys": {
                        key: store.values[key].etag for key in ("market.sqlite", "runs.sqlite")
                    },
                }
            ).encode(),
        )
        return sources.canonical_generations(store)

    return store, database, publish, old, regulation


def _new_stage(tmp_path, store, database):
    raw = tmp_path / "new-source.sqlite"
    bundle = earnings(raw, STAMP, "2026-10-20", "130A")
    packet = tmp_path / "stage"
    packet.mkdir()
    selected = export_handoff(
        raw, packet / "receipts.sqlite", since=STAMP, collection="earnings_calendar"
    )
    (packet / "result.json").write_text(
        json.dumps(
            {
                "source": "jpx-earnings",
                "asof": str(DAY),
                "started_at": STAMP.isoformat(),
                "bundle_ids": selected,
            }
        )
    )
    save_handoff(store, "jpx-earnings-new", packet, market=True)
    entry = available_handoffs(store, market=True)[0]
    replay = import_handoff(
        packet / "receipts.sqlite", database, selected, collection="earnings_calendar"
    )
    assert replay.complete
    return bundle, entry, replay


def _seal_command(tmp_path, monkeypatch, store, database, entry, replay, *, capture=True):
    work = tmp_path / "integration"
    work.mkdir()
    report = {
        "handoffs": [{"entry": entry, "result": replay.report, "applied": True, "recovery": False}]
    }
    if capture:
        report["jpx_publication"] = verification.capture_publication(
            database, replay.report["retained_bundles"]
        )
    (work / "applied.json").write_text(json.dumps(report))
    handle, _ = acquire(store, "operator")
    lease = tmp_path / "lease.json"
    _save_handle(lease, handle)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: store)
    return ["seal", "--directory", str(work), "--lease-handle", str(lease)], work


@pytest.mark.parametrize("capture", [False, True])
def test_unpublished_jpx_cannot_be_sealed_against_a_coherent_old_bundle(
    tmp_path, monkeypatch, publication, capture
):
    store, database, publish, _, _ = publication
    publish(identity="old")
    remote_before = store.values["market.sqlite"]
    _, entry, replay = _new_stage(tmp_path, store, database)
    command, _ = _seal_command(
        tmp_path, monkeypatch, store, database, entry, replay, capture=capture
    )
    store.put_keys.clear()
    assert sources.main(command) == 1
    assert store.put_keys == []
    assert store.values["market.sqlite"] == remote_before
    assert available_handoffs(store, market=True) == [entry]


def test_successful_readback_seals_and_resume_preserves_exact_marker(
    tmp_path, monkeypatch, publication
):
    store, database, publish, _, _ = publication
    bundle, entry, replay = _new_stage(tmp_path, store, database)
    command, work = _seal_command(tmp_path, monkeypatch, store, database, entry, replay)
    generations = publish()
    assert sources.main(command) == 0
    marker_key = stage_prefix(entry["stage_id"], market=True) + "/integrated.json"
    marker = store.values[marker_key]
    assert json.loads(marker.body)["canonical_generations"] == generations
    report = json.loads((work / "applied.json").read_text())
    assert report["jpx_readback"]["jpx.source_bundles"] == 1
    assert report["jpx_readback"]["jpx.earnings_calendar"] == 1
    assert bundle in replay.report["retained_bundles"]
    assert available_handoffs(store, market=True) == []
    store.put_keys.clear()
    assert sources.main(command) == 0
    assert store.put_keys == []
    assert store.values[marker_key] == marker


@pytest.mark.parametrize("changed", ["body", "projection", "missing_original"])
def test_same_count_body_or_projection_changes_cannot_acknowledge_jpx(
    tmp_path, publication, changed
):
    store, database, publish, bundle, _ = publication
    expected = verification.capture_publication(database, [bundle])
    wrong = tmp_path / "wrong.sqlite"
    shutil.copyfile(database, wrong)
    with closing(open_connection(wrong)) as conn, conn:
        if changed == "body":
            conn.execute(
                "UPDATE jpx_source_responses SET body_gzip=? WHERE bundle_id=?",
                (b"different bytes", bundle),
            )
        elif changed == "projection":
            conn.execute("UPDATE jpx_earnings_calendar SET ticker='130A'")
        else:
            conn.execute("DELETE FROM jpx_source_responses WHERE bundle_id=?", (bundle,))
            conn.execute("DELETE FROM jpx_source_bundles WHERE bundle_id=?", (bundle,))
    generations = publish(wrong)
    with pytest.raises(LakePublishError, match="readback differs"):
        verification.verify_publication(
            store, expected=expected, directory=tmp_path / "read", generations=generations
        )


def test_empty_regulation_day_is_checked_and_other_days_do_not_replace_its_scope(
    tmp_path, publication
):
    store, database, publish, _, bundle = publication
    with closing(open_connection(database)) as conn, conn:
        row = conn.execute("SELECT * FROM jpx_regulation_flags LIMIT 1").fetchone()
        conn.execute("DELETE FROM jpx_regulation_flags WHERE asof_date=?", (str(DAY),))
        conn.execute(
            "INSERT INTO jpx_regulation_flags VALUES(?,?,?,?,?)",
            (str(DAY - timedelta(days=1)), *row[1:]),
        )
    expected = verification.capture_publication(database, [bundle])
    generations = publish()
    arguments = {"expected": expected, "directory": tmp_path / "read", "generations": generations}
    assert verification.verify_publication(store, **arguments)["jpx.regulation_flags"] == 0
    with closing(open_connection(database)) as conn, conn:
        conn.execute("INSERT INTO jpx_regulation_flags VALUES(?,?,?,?,?)", row)
    arguments["generations"] = publish(identity="not-empty")
    with pytest.raises(LakePublishError, match="readback differs"):
        verification.verify_publication(store, **arguments)


def test_applied_bundle_readback_does_not_require_unrelated_monthly_history(tmp_path, publication):
    store, database, publish, bundle, _ = publication
    expected = verification.capture_publication(database, [bundle])
    newer = regulations(database, DAY + timedelta(days=1), STAMP + timedelta(days=1))
    rebuild_jpx_snapshot(database, newer)
    generations = publish()
    counts = verification.verify_publication(
        store, expected=expected, directory=tmp_path / "read", generations=generations
    )
    assert counts["jpx.source_bundles"] == 1
    assert counts["jpx.source_responses"] == 2


def test_explicit_recovery_keeps_the_original_failure_marker(tmp_path, monkeypatch, publication):
    store, database, publish, _, _ = publication
    _, entry, replay = _new_stage(tmp_path, store, database)
    seal_integrated(store, entry, generations={"lake_release_id": "before"}, recovery=True)
    original_key = stage_prefix(entry["stage_id"], market=True) + "/integrated.json"
    original = store.values[original_key]
    entry["explicit_jpx_recovery"] = True
    command, _ = _seal_command(tmp_path, monkeypatch, store, database, entry, replay)
    generations = publish()
    assert sources.main(command) == 0
    recovered = (
        original_key.removesuffix("integrated.json") + f"recovered-{entry['manifest_sha256']}.json"
    )
    assert json.loads(store.values[recovered].body)["canonical_generations"] == generations
    assert store.values[original_key] == original


def test_lease_lost_after_readback_stops_before_the_first_jpx_marker(
    tmp_path, monkeypatch, publication
):
    from baibai_jobs.publication.lease import _load_handle

    store, database, publish, _, _ = publication
    _, entry, replay = _new_stage(tmp_path, store, database)
    command, _ = _seal_command(tmp_path, monkeypatch, store, database, entry, replay)
    publish()
    verify = sources.verify_jpx_publication

    def release_after_readback(*args, **kwargs):
        result = verify(*args, **kwargs)
        release(store, _load_handle(tmp_path / "lease.json"))
        return result

    monkeypatch.setattr(sources, "verify_jpx_publication", release_after_readback)
    store.put_keys.clear()
    assert sources.main(command) == 1
    assert not any(key.endswith("/integrated.json") for key in store.put_keys)
    assert available_handoffs(store, market=True) == [entry]


@pytest.mark.parametrize("missing_selected_original", [False, True])
def test_delayed_earnings_seal_requires_the_newer_selected_original(
    tmp_path, monkeypatch, publication, missing_selected_original
):
    store, database, publish, _, _ = publication
    # The arriving October stage must not replace the already retained November view.
    selected = earnings(database, STAMP + timedelta(days=20), "2026-11-07", "7203")
    rebuild_jpx_snapshot(database, selected)
    retained, entry, replay = _new_stage(tmp_path, store, database)
    assert replay.report["retained_bundles"] == [retained]
    assert replay.report["replayed_bundles"] == [selected]

    edinet = empty_edinet_input(store, tmp_path)
    seed_calendar(store, database, tmp_path / "calendar", as_of=DAY)
    seed_master(store, database, tmp_path / "master", as_of=DAY)
    apply_work = tmp_path / "apply"
    apply_work.mkdir()
    (apply_work / "plan.json").write_text(
        json.dumps({"handoffs": [entry], "edinet_input": edinet.model_dump(mode="json")})
    )
    applied = sources.apply(store, apply_work, market=database, asof=DAY)
    assert applied["handoffs"][0]["applied"]
    assert not applied["handoffs"][0]["recovery"]
    raw_scope = applied["jpx_publication"]["jpx.source_bundles"]
    assert set(raw_scope["bundle_ids"]) == {retained, selected}
    assert raw_scope["months"] == ["2026-10", "2026-11"]

    command, seal_work = _seal_command(
        tmp_path, monkeypatch, store, database, entry, replay, capture=False
    )
    (seal_work / "applied.json").write_text(json.dumps(applied))
    candidate = database
    if missing_selected_original:
        candidate = tmp_path / "missing-selected.sqlite"
        shutil.copyfile(database, candidate)
        with closing(open_connection(candidate)) as conn, conn:
            conn.execute("DELETE FROM jpx_source_responses WHERE bundle_id=?", (selected,))
            conn.execute("DELETE FROM jpx_source_bundles WHERE bundle_id=?", (selected,))
            # The count and latest view still look coherent without its selected L1.
            assert conn.execute("SELECT * FROM jpx_earnings_calendar").fetchall() == [
                ("2026-11-07", "7203")
            ]
    publish(candidate)
    store.put_keys.clear()
    assert sources.main(command) == int(missing_selected_original)
    marker = stage_prefix(entry["stage_id"], market=True) + "/integrated.json"
    if missing_selected_original:
        assert store.put_keys == []
        assert marker not in store.values
        assert available_handoffs(store, market=True) == [entry]
    else:
        assert json.loads(store.values[marker].body)["status"] == "integrated"
        assert available_handoffs(store, market=True) == []
