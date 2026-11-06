"""固定TV原本の有限回復。実原本・ネットワーク・本番credentialは使わない。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from tests.tools import test_inspect_legacy_economic_handoffs as inspection_tests
from tools.diagnostics import recover_legacy_tradingview_handoff as recovery

from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_jobs.acquisition.economic_staging import stage_prefix
from baibai_jobs.integration.native_source_merge import verify_stores
from baibai_jobs.publication.lease import _save_handle, acquire


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store, targets = inspection_tests.setup.__wrapped__(tmp_path)
    monkeypatch.setattr(recovery, "TARGET", targets[1])
    handle, _ = acquire(store, "operator")
    path = tmp_path / "lease.json"
    _save_handle(path, handle)
    return store, targets, path


def test_recovers_only_tv_preserves_base_frb_and_markers(tmp_path, setup):
    store, targets, lease = setup
    before = {key: item.body for key, item in store.values.items()}
    puts = len(store.put_keys)
    gets = len(store.get_keys)
    work = tmp_path / "recover"
    report = recovery.recover(store, work, lease_handle=lease)
    assert report["status"] == "recovered"
    assert report["already_present"] is False
    assert report["canonical_readback_verified"] is True
    assert report["backup_readback_verified"] is True
    assert report["originals_unchanged"] is True
    assert store.put_keys[puts:] == ["indicators.sqlite.bak", "indicators.sqlite"]
    assert store.values["indicators.sqlite.bak"].body == before["indicators.sqlite"]
    for key, value in before.items():
        if key != "indicators.sqlite":
            assert store.values[key].body == value
    with closing(sqlite3.connect(work / "readback.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM economic_source_documents").fetchone()[0] == 3
        # The fixture FRB failed run exists only in its original stage, never imported.
        assert conn.execute("SELECT COUNT(*) FROM economic_source_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 1
    verify_stores(work / "inspection/canonical.sqlite", work / "readback.sqlite")
    verify_stores(work / "inspection/converted-0/receipts.sqlite", work / "readback.sqlite")
    assert report["frb_included"] is report["marker_updated"] is False
    assert report["inputs_published"] is report["l2_published"] is False
    assert inspection_tests.SECRET_SENTINEL not in json.dumps(report)
    assert store.values["indicators.sqlite"].etag not in json.dumps(report)
    assert all(
        targets[0].stage_id not in key
        for key in store.get_keys[gets:]
        if key.startswith("coordination/economic-sources/")
    )


def test_repeat_is_noop_by_logical_originals_not_sqlite_layout(tmp_path, setup):
    store, _, lease = setup
    first = recovery.recover(store, tmp_path / "first", lease_handle=lease)
    assert first["status"] == "recovered"
    before = {key: item.body for key, item in store.values.items()}
    puts = len(store.put_keys)
    second = recovery.recover(store, tmp_path / "second", lease_handle=lease)
    assert second["status"] == "already_present"
    assert second["canonical_readback_verified"] is True
    assert second["canonical_put_attempted"] is second["backup_put_attempted"] is False
    assert len(store.put_keys) == puts
    assert {key: item.body for key, item in store.values.items()} == before


def test_failed_inspection_never_publishes(tmp_path, setup):
    store, targets, lease = setup
    marker = stage_prefix(targets[1].stage_id) + "/integrated.json"
    store.store_object(marker, b'{"invalid":"private-sentinel"}')
    puts = len(store.put_keys)
    report = recovery.recover(store, tmp_path / "failed", lease_handle=lease)
    assert report["status"] == "failed"
    assert report["inspection"]["status"] == "failed"
    assert report["canonical_put_attempted"] is report["backup_put_attempted"] is False
    assert len(store.put_keys) == puts
    assert "private-sentinel" not in json.dumps(report)


@pytest.mark.parametrize("changed", ["lease", "canonical", "marker"])
def test_race_before_publication_cannot_overwrite(tmp_path, setup, monkeypatch, changed):
    store, targets, lease = setup
    real = recovery.inspect

    def inspect(*args, **kwargs):
        result = real(*args, **kwargs)
        key = {
            "lease": "coordination/publication-lease.json",
            "canonical": "indicators.sqlite",
            "marker": stage_prefix(targets[1].stage_id) + "/integrated.json",
        }[changed]
        store.store_object(key, b"changed-by-another-writer")
        return result

    monkeypatch.setattr(recovery, "inspect", inspect)
    puts = len(store.put_keys)
    report = recovery.recover(store, tmp_path / "raced", lease_handle=lease)
    assert report["status"] == "failed"
    assert report["canonical_put_attempted"] is report["backup_put_attempted"] is False
    assert len(store.put_keys) == puts


@pytest.mark.parametrize("after_write", [False, True])
def test_uncertain_put_is_resolved_only_by_readback(tmp_path, setup, monkeypatch, after_write):
    store, _, lease = setup
    real = store.put_file

    def put(key, path, **kwargs):
        if key == "indicators.sqlite":
            if after_write:
                real(key, path, **kwargs)
            raise LakePublishError("private provider error")
        return real(key, path, **kwargs)

    monkeypatch.setattr(store, "put_file", put)
    report = recovery.recover(store, tmp_path / "uncertain", lease_handle=lease)
    assert report["canonical_put_attempted"] is True
    assert report["canonical_readback_verified"] is after_write
    assert report["status"] == ("recovered" if after_write else "failed")
    assert "private provider error" not in json.dumps(report)


def test_put_then_readback_failure_is_not_success_and_does_not_retry(tmp_path, setup, monkeypatch):
    store, _, lease = setup
    real_put, real_download = store.put_file, store.download_file
    attempted = []

    def put(key, path, **kwargs):
        result = real_put(key, path, **kwargs)
        if key == "indicators.sqlite":
            attempted.append(key)
        return result

    def download(key, path, **kwargs):
        if key == "indicators.sqlite" and attempted:
            raise LakePublishError("private readback failure")
        return real_download(key, path, **kwargs)

    monkeypatch.setattr(store, "put_file", put)
    monkeypatch.setattr(store, "download_file", download)
    work = tmp_path / "readback-failed"
    report = recovery.recover(store, work, lease_handle=lease)
    assert report["status"] == "failed"
    assert report["canonical_put_attempted"] is True
    assert report["canonical_readback_verified"] is False
    assert attempted == ["indicators.sqlite"]
    assert sha256_file(work / "upload.sqlite") == report["candidate_sha256"]
    assert (work / "inspection/raw-0/receipts.sqlite").is_file()


def test_existing_directory_is_not_reused(tmp_path, setup):
    store, _, lease = setup
    work = tmp_path / "keep"
    work.mkdir()
    (work / "original").write_text("unchanged")
    puts = len(store.put_keys)
    with pytest.raises(FileExistsError):
        recovery.recover(store, work, lease_handle=lease)
    assert (work / "original").read_text() == "unchanged"
    assert len(store.put_keys) == puts


def test_workflow_is_finite_and_uses_existing_publication_lease():
    path = Path(__file__).parents[2] / ".github/workflows/recover-legacy-tradingview-handoff.yml"
    text = path.read_text()
    value = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(value["on"]) == {"workflow_dispatch"}
    assert value["concurrency"]["group"] == "cloud-publish"
    assert value["concurrency"]["cancel-in-progress"] == "false"
    assert "acquire --purpose operator" in text
    assert "always() && steps.lease-acquire.outputs.acquired == 'true'" in text
    assert "publication.lease release" in text
    assert "upload-artifact" not in text
    assert "seal_integrated" not in text
    assert "cloud-daily" not in text


def test_backup_readback_failure_prevents_canonical_put(tmp_path, setup, monkeypatch):
    store, _, lease = setup
    real = store.download_file
    puts = len(store.put_keys)
    before = store.values["indicators.sqlite"].body

    def download(key, path, **kwargs):
        if key == "indicators.sqlite.bak":
            raise LakePublishError("private backup readback failure")
        return real(key, path, **kwargs)

    monkeypatch.setattr(store, "download_file", download)
    report = recovery.recover(store, tmp_path / "backup-failed", lease_handle=lease)
    assert report["status"] == "failed"
    assert report["backup_put_attempted"] is True
    assert report["backup_readback_verified"] is False
    assert report["canonical_put_attempted"] is False
    assert store.put_keys[puts:] == ["indicators.sqlite.bak"]
    assert store.values["indicators.sqlite"].body == before


def test_marker_change_after_confirmed_put_is_reported_without_rewriting_it(
    tmp_path, setup, monkeypatch
):
    store, targets, lease = setup
    real = store.put_file
    marker = stage_prefix(targets[1].stage_id) + "/integrated.json"

    def put(key, path, **kwargs):
        result = real(key, path, **kwargs)
        if key == "indicators.sqlite":
            store.store_object(marker, b"changed-by-another-writer")
        return result

    monkeypatch.setattr(store, "put_file", put)
    report = recovery.recover(store, tmp_path / "marker-after", lease_handle=lease)
    assert report["status"] == "failed"
    assert report["canonical_put_attempted"] is True
    assert report["canonical_readback_verified"] is True
    assert report["marker_updated"] is False
    assert store.values[marker].body == b"changed-by-another-writer"


def test_noop_readback_cannot_claim_another_canonical_generation(tmp_path, setup, monkeypatch):
    store, _, lease = setup
    assert recovery.recover(store, tmp_path / "first", lease_handle=lease)["status"] == "recovered"
    real = recovery._read
    puts = len(store.put_keys)

    def read(store, path):
        store.store_object(
            "indicators.sqlite", store.values["indicators.sqlite"].body + b"\x00" * 4096
        )
        return real(store, path)

    monkeypatch.setattr(recovery, "_read", read)
    report = recovery.recover(store, tmp_path / "second", lease_handle=lease)
    assert report["status"] == "failed"
    assert report["already_present"] is True
    assert report["canonical_readback_verified"] is False
    assert len(store.put_keys) == puts
