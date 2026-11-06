"""二stage限定のread-only診断。実R2・実原本・外部取得は使わない。"""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.tradingview_market import capture
from tools.diagnostics import inspect_legacy_economic_handoffs as diagnosis

from baibai_engine.data_collection.providers.tradingview.market_receipts import (
    retain_market_capture,
)
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition.economic_staging import save_handoff, stage_prefix
from baibai_jobs.acquisition.receipts import available_handoffs, seal_integrated

AT = datetime(2026, 11, 4, 22, 59, 26, 715745, tzinfo=UTC)
SECRET_SENTINEL = "synthetic-private-provider-payload-do-not-log"


@pytest.fixture
def setup(tmp_path):
    store = MemoryR2Store()
    canonical = tmp_path / "source-canonical.sqlite"
    with closing(initialize_database(canonical)) as conn, conn:
        store_snapshot(
            conn,
            source_id="fed-h15",
            observed_at=date(2026, 11, 3),
            fetched_at=AT + timedelta(days=1),
            records=[{"value": "newer-canonical", "private": SECRET_SENTINEL}],
        )
    store.store_object("indicators.sqlite", canonical.read_bytes())
    targets = []
    for group, source, status in (
        ("frb_h15", "fed-h15", "failed"),
        ("tradingview_market", "tradingview-market", "ok"),
    ):
        packet = tmp_path / group
        packet.mkdir()
        database = packet / "receipts.sqlite"
        outcome = {"source_id": source, "status": status}
        report = {"group": group, "started_at": AT.isoformat(), "asof": "2026-11-05"}
        with closing(initialize_database(database)) as conn, conn:
            if group == "frb_h15":
                store_snapshot(
                    conn,
                    source_id=source,
                    observed_at=date(2026, 11, 3),
                    fetched_at=AT,
                    records=[{"value": "older-receipt", "private": SECRET_SENTINEL}],
                )
                conn.execute(
                    "INSERT INTO economic_source_runs VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        "synthetic-failed-run",
                        source,
                        "2026-11-03",
                        "2026-11-05",
                        AT.isoformat(),
                        AT.isoformat(),
                        "failed",
                        0,
                        SECRET_SENTINEL,
                    ),
                )
                outcome["history_recovery_required"] = False
            else:
                captures = [
                    capture(receipt_id=f"synthetic-{i}", bars=True, received=AT) for i in range(3)
                ]
                for item in captures:
                    retain_market_capture(conn, item)
                report["market_receipts"] = [
                    {"receipt_id": item.receipt_id, "sha256": item.digest()} for item in captures
                ]
            conn.execute("DROP TABLE economic_source_record_confirmations")
            conn.execute("DROP TABLE economic_source_collection_confirmations")
            conn.execute("PRAGMA user_version=12")
        report["sources"] = [outcome]
        (packet / "result.json").write_text(json.dumps(report))
        stage = group + "-synthetic-legacy"
        save_handoff(store, stage, packet)
        manifest = store.get_bytes(stage_prefix(stage) + "/handoff.json")
        targets.append(
            diagnosis.Target(group, stage, hashlib.sha256(manifest).hexdigest(), source, status)
        )
    for entry in available_handoffs(store):
        seal_integrated(store, entry, generations={"synthetic": "only"}, recovery=True)
    return store, tuple(targets)


def test_prepares_only_local_copy_keeps_failure_latest_value_and_remote_bytes(tmp_path, setup):
    store, targets = setup
    before = {key: value.body for key, value in store.values.items()}
    puts = list(store.put_keys)
    work = tmp_path / "inspection"
    report = diagnosis.inspect(store, work, targets=targets)
    assert report["status"] == "prepared_locally"
    assert report["remote_write"] is report["raw_upload"] is report["recovery_published"] is False
    assert report["remote_objects_unchanged"] is True
    assert [item["acquisition_status"] for item in report["handoffs"]] == ["failed", "ok"]
    assert all(item["marker"]["status"] == "needs_explicit_recovery" for item in report["handoffs"])
    assert report["handoffs"][1]["capture_count"] == 3
    assert before == {key: value.body for key, value in store.values.items()}
    assert store.put_keys == puts
    with closing(sqlite3.connect(work / "candidate-1.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM economic_source_documents").fetchone()[0] == 3
        assert conn.execute("SELECT status FROM economic_source_runs").fetchone()[0] == "failed"
        assert conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 2
        assert conn.execute(
            "SELECT fetched_at FROM economic_source_records ORDER BY fetched_at DESC LIMIT 1"
        ).fetchone()[0] == (AT + timedelta(days=1)).isoformat(timespec="microseconds")
    summary = json.dumps(report)
    assert SECRET_SENTINEL not in summary
    assert store.values["indicators.sqlite"].etag not in summary
    assert not list(work.rglob("market-origin.json"))
    assert (work.stat().st_mode & 0o777) == 0o700


def test_wrong_fixed_manifest_fails_without_writes(tmp_path, setup):
    store, targets = setup
    bad = diagnosis.Target(targets[0].group, targets[0].stage_id, "0" * 64, "fed-h15", "failed")
    puts = list(store.put_keys)
    report = diagnosis.inspect(store, tmp_path / "bad", targets=(bad, targets[1]))
    assert report["status"] == "failed"
    assert report["reason"] == "LakePublishError"
    assert not report["handoffs"]
    assert store.put_keys == puts


def test_each_manifest_get_requires_original_digest(setup):
    store, targets = setup
    reader = diagnosis.ReadOnlyInspectionStore(store, targets)
    key = stage_prefix(targets[0].stage_id) + "/handoff.json"
    reader.get_bytes(key)
    store.store_object(key, b'{"changed":true}')
    with pytest.raises(LakePublishError, match="fixed inspection manifest"):
        reader.get_bytes(key)


def test_canonical_change_during_reconcile_does_not_claim_prepared(tmp_path, setup, monkeypatch):
    store, targets = setup
    real = diagnosis.reconcile_stores

    def changed(*args, **kwargs):
        result = real(*args, **kwargs)
        store.store_object("indicators.sqlite", b"changed-after-download")
        return result

    monkeypatch.setattr(diagnosis, "reconcile_stores", changed)
    report = diagnosis.inspect(store, tmp_path / "changed", targets=targets)
    assert report["status"] == "failed"
    assert report["reason"] == "LakePublishError"
    assert report["recovery_published"] is False
    assert "candidate" not in report


@pytest.mark.parametrize("present", [True, False])
def test_canonical_metadata_digest_is_checked_or_explicitly_unknown(tmp_path, setup, present):
    store, targets = setup
    if present:
        store.values["indicators.sqlite"].metadata["sha256"] = "0" * 64
    else:
        store.values["indicators.sqlite"].metadata.pop("sha256")
    report = diagnosis.inspect(store, tmp_path / "metadata", targets=targets)
    if present:
        assert report["status"] == "failed"
    else:
        assert report["status"] == "prepared_locally"
        assert report["canonical"]["metadata_sha256_verified"] is False


def test_marker_identity_mismatch_is_not_rewritten(tmp_path, setup):
    store, targets = setup
    key = stage_prefix(targets[0].stage_id) + "/integrated.json"
    value = json.loads(store.values[key].body)
    value["manifest_sha256"] = "0" * 64
    store.store_object(key, json.dumps(value).encode())
    before = store.values[key].body
    report = diagnosis.inspect(store, tmp_path / "marker", targets=targets)
    assert report["status"] == "failed"
    assert store.values[key].body == before


def test_market_receipt_collision_cannot_be_overwritten(tmp_path, setup):
    store, targets = setup
    path = tmp_path / "collision.sqlite"
    path.write_bytes(store.values["indicators.sqlite"].body)
    with closing(initialize_database(path)) as conn:
        retain_market_capture(
            conn, capture(receipt_id="synthetic-0", bars=True, received=AT + timedelta(days=1))
        )
    store.store_object("indicators.sqlite", path.read_bytes())
    before = store.values["indicators.sqlite"].body
    report = diagnosis.inspect(store, tmp_path / "collision", targets=targets)
    assert report["status"] == "failed"
    assert report["reason"] == "ValueError"
    assert store.values["indicators.sqlite"].body == before


def test_wrapper_blocks_other_objects_and_all_remote_writes(tmp_path, setup):
    store, targets = setup
    reader = diagnosis.ReadOnlyInspectionStore(store, targets)
    puts = list(store.put_keys)
    for key in (
        "application.sqlite",
        "market.sqlite",
        "coordination/economic-sources/other/handoff.json",
    ):
        with pytest.raises(ValueError, match="outside"):
            reader.head(key)
    with pytest.raises(PermissionError, match="read-only"):
        reader.put_file(
            "indicators.sqlite", tmp_path / "not-read", sha256="", content_md5="", content_type=""
        )
    assert store.put_keys == puts


def test_existing_directory_is_not_reused_or_erased(tmp_path, setup):
    store, targets = setup
    work = tmp_path / "already"
    work.mkdir()
    (work / "keep").write_text("preserve")
    before = list(store.get_keys)
    with pytest.raises(FileExistsError):
        diagnosis.inspect(store, work, targets=targets)
    assert store.get_keys == before
    assert (work / "keep").read_text() == "preserve"


def test_workflow_is_manual_read_only_without_raw_upload():
    path = Path(__file__).parents[2] / ".github/workflows/inspect-legacy-economic-handoffs.yml"
    text = path.read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "upload-artifact" not in text
    assert "DISCORD" not in text
    assert "pull_request_target" not in text
    assert "push-indicators" not in text
    assert "save_handoff" not in text
