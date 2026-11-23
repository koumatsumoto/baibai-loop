"""The zstd-only FRED inbox, tested without external or production access."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from shutil import copyfile

import pytest
from compression import zstd
from tests.helpers.native_handoff import integrate_economic
from tests.helpers.r2_store import VerifiedMemoryR2Store

import baibai_jobs.acquisition.economic_staging as staging
import baibai_jobs.integration.economic_handoff as handoff
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, R2Error
from baibai_jobs.acquisition.economic_staging import (
    FRED_HANDOFF_NAMESPACE,
    load_handoff,
    save_fred_handoff,
    save_handoff,
    stage_prefix,
)
from baibai_jobs.acquisition.receipts import available_handoffs, download_fixed, seal_integrated
from baibai_jobs.integration.economic_handoff import validate_handoff
from baibai_jobs.integration.native_source_merge import merge_stores, verify_stores

FORMAT = "fred-acquisition-receipts-zstd-v1"
SOURCE = "fred-ecbassetsw"
STAGE = "fred_csv-2026-11-22-42-1"
ASOF = date(2026, 11, 22)
CLOCK = datetime(2026, 11, 22, 3, 0, 1, 893732, tzinfo=UTC)


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


class VerifiedReads(VerifiedMemoryR2Store):
    def __init__(self):
        super().__init__()
        self.verified_reads = []

    def get_verified_object(self, key, *, sha256, max_bytes):
        self.verified_reads.append((key, sha256, max_bytes))
        return super().get_verified_object(key, sha256=sha256, max_bytes=max_bytes)


@pytest.fixture
def packet(tmp_path):
    start = date(2026, 10, 1)
    end = date(2026, 11, 1)
    canonical = tmp_path / "canonical.sqlite"
    with closing(initialize_database(canonical)) as conn, conn:
        confirm_source_response(
            conn,
            source_id=SOURCE,
            fetched_at=CLOCK - timedelta(hours=1),
            records_by_day={start: [{"value": "unchanged"}], end: [{"value": "no longer present"}]},
        )
    incoming = tmp_path / "incoming.sqlite"
    copyfile(canonical, incoming)
    with closing(sqlite3.connect(incoming)) as conn, conn:
        confirm_source_response(
            conn,
            source_id=SOURCE,
            fetched_at=CLOCK,
            records_by_day={start: [{"value": "unchanged"}]},
        )
    directory = tmp_path / "packet"
    directory.mkdir()
    export_receipts(incoming, directory / "receipts.sqlite", source_ids=(SOURCE,), since=CLOCK)
    (directory / "result.json").write_text(
        json.dumps(
            {
                "group": "fred_csv",
                "started_at": CLOCK.isoformat(),
                "asof": str(ASOF),
                "sources": [{"source_id": SOURCE, "status": "ok", "record_count": 1}],
            }
        ),
        encoding="utf-8",
    )
    return canonical, directory


def live_prefix(stage=STAGE):
    return f"coordination/economic-fred-sources/{stage}"


def validate_closed(directory):
    validate_handoff(directory, "fred_csv", standalone=True)


def save_live(store, directory, *, stage=STAGE, validate=validate_closed):
    return save_fred_handoff(store, stage, directory, validate_packet=validate)


def load_live(store, output, *, stage=STAGE, expected=None):
    if expected is None:
        expected = digest(store.values[f"{live_prefix(stage)}/handoff.json"].body)
    load_handoff(
        store, stage, output, namespace=FRED_HANDOFF_NAMESPACE, expected_manifest_sha256=expected
    )


def compressed(raw, result, *, stage=STAGE, wire=None, prefix=None, **overrides):
    wire = zstd.compress(raw, level=1) if wire is None else wire
    prefix = live_prefix(stage) if prefix is None else prefix
    manifest = {
        "receipt_format": FORMAT,
        "receipt_sha256": digest(raw),
        "receipt_bytes": len(raw),
        "stored_sha256": digest(wire),
        "stored_bytes": len(wire),
        "result": result,
        **overrides,
    }
    manifest.setdefault("receipt_key", f"{prefix}/receipts-{manifest['receipt_sha256']}.sqlite.zst")
    store = VerifiedReads()
    store.store_object(f"{prefix}/handoff.json", json.dumps(manifest, sort_keys=True).encode())
    store.store_object(manifest["receipt_key"], wire, content_type="application/zstd")
    return store, manifest


@pytest.mark.parametrize("existing_empty", [False, True])
def test_only_fixed_zstd_is_live_and_replay_keeps_the_raw_result(
    tmp_path, packet, monkeypatch, existing_empty
):
    monkeypatch.setattr(staging, "_FRED_DECODE_CHUNK", 127)
    original, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    result = json.loads((directory / "result.json").read_bytes())
    store = VerifiedReads()
    manifest_sha = save_live(store, directory)
    key = f"{live_prefix()}/handoff.json"
    assert manifest_sha == digest(store.values[key].body)
    manifest = json.loads(store.values[key].body)
    assert manifest["receipt_format"] == FORMAT
    assert manifest["receipt_sha256"] == digest(raw)
    assert manifest["receipt_bytes"] == len(raw)
    assert manifest["stored_bytes"] + 512 < len(raw)
    assert set(store.values) == {key, manifest["receipt_key"]}
    [entry] = available_handoffs(store, market=False, source="economic-fred_csv")
    assert entry["namespace"] == FRED_HANDOFF_NAMESPACE
    assert entry["manifest_sha256"] == manifest_sha
    restored = tmp_path / "new"
    if existing_empty:
        restored.mkdir()
    heads, puts = len(store.head_keys), len(store.put_keys)
    download_fixed(store, entry, restored)
    assert store.verified_reads == [
        (manifest["receipt_key"], manifest["stored_sha256"], manifest["stored_bytes"])
    ]
    assert manifest["receipt_key"] not in store.head_keys[heads:]
    assert len(store.put_keys) == puts
    assert {path.name for path in restored.iterdir()} == {"result.json", "receipts.sqlite"}
    assert (restored / "receipts.sqlite").read_bytes() == raw
    assert json.loads((restored / "result.json").read_bytes()) == result
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in restored.iterdir())
    assert restored.stat().st_mode & 0o777 == 0o700

    replays = []
    for packet_dir in (directory, restored):
        canonical = packet_dir / "isolated.sqlite"
        copyfile(original, canonical)
        selected = packet_dir / "selected.sqlite"
        validate_handoff(packet_dir, "fred_csv", SOURCE)
        replay = accept_rechecks(packet_dir / "receipts.sqlite", canonical, selected)
        merge_stores(selected, canonical)
        verify_stores(selected, canonical)
        replays.append((replay, selected.read_bytes(), canonical.read_bytes()))
    assert replays[0] == replays[1]
    seal_integrated(store, entry, generations={}, recovery=False)
    marker = json.loads(store.values[f"{live_prefix()}/integrated.json"].body)
    assert marker["status"] == "integrated"
    assert marker["manifest_sha256"] == manifest_sha
    assert not available_handoffs(store, market=False)


@pytest.mark.parametrize(
    "changes",
    [
        {"result": {"group": "boj"}},
        {"planning_key": "not-allowed"},
        {"receipt_bytes": 128 * 1024**2 + 1},
        {"stored_bytes": True},
        {"stored_sha256": "short"},
        {"receipt_sha256": "short"},
        {"receipt_key": "coordination/economic-sources/other/receipts.sqlite.zst"},
        {"receipt_format": "fred-acquisition-receipts-zstd-v2"},
    ],
)
def test_live_does_not_read_payload_for_invalid_descriptor(tmp_path, packet, changes):
    _, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    result = changes.get("result") or json.loads((directory / "result.json").read_bytes())
    store, _ = compressed(raw, result, **{k: v for k, v in changes.items() if k != "result"})
    output = tmp_path / "output"
    with pytest.raises((LakePublishError, ValueError)):
        load_live(store, output)
    assert not store.verified_reads
    assert not output.exists()


@pytest.mark.parametrize("stage", ["boj-2026-11-22-42-1", "fred_csv-"])
def test_fred_codec_cannot_be_relabelled_under_other_stages(tmp_path, packet, stage):
    _, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    result = json.loads((directory / "result.json").read_bytes())
    store, _ = compressed(raw, result, stage=stage)
    with pytest.raises(ValueError, match="requires a FRED economic acquisition stage"):
        load_live(store, tmp_path / "output", stage=stage)
    assert not store.verified_reads


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "stored_sha",
        "stored_size",
        "stored_over_budget",
        "frame",
        "truncated",
        "trailing",
        "two_frames",
        "raw_sha",
        "raw_size_short",
        "raw_size_long",
        "not_sqlite",
        "large_window",
    ],
)
def test_invalid_fred_transport_never_installs_packet_or_reads_legacy_fallback(
    tmp_path, packet, damage
):
    _, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    result = json.loads((directory / "result.json").read_bytes())
    wire = zstd.compress(raw, level=1)
    changes = {}
    if damage == "stored_sha":
        changes["stored_sha256"] = "0" * 64
    elif damage == "stored_size":
        changes["stored_bytes"] = len(wire) + 1
    elif damage == "stored_over_budget":
        changes["stored_bytes"] = len(wire) - 1
    elif damage == "frame":
        wire = b"not-a-frame"
    elif damage == "truncated":
        wire = wire[:-1]
    elif damage == "trailing":
        wire += b"unwanted"
    elif damage == "two_frames":
        wire += zstd.compress(b"second")
    elif damage == "raw_sha":
        changes["receipt_sha256"] = "0" * 64
    elif damage == "raw_size_short":
        changes["receipt_bytes"] = len(raw) - 1
    elif damage == "raw_size_long":
        changes["receipt_bytes"] = len(raw) + 1
    elif damage == "not_sqlite":
        raw = b"X" * len(raw)
        wire = zstd.compress(raw, level=1)
    elif damage == "large_window":
        raw = b"X" * (10 * 1024**2)
        wire = zstd.compress(raw, options={zstd.CompressionParameter.window_log: 24})
    store, manifest = compressed(raw, result, wire=wire, **changes)
    if damage == "missing":
        del store.values[manifest["receipt_key"]]
    fallback = f"{live_prefix()}/receipts-{manifest['receipt_sha256']}.sqlite"
    store.store_object(fallback, raw)
    output = tmp_path / "output"
    with pytest.raises(LakePublishError):
        load_live(store, output)
    assert fallback not in store.get_keys
    assert fallback not in store.head_keys
    assert not output.exists()
    assert not list(tmp_path.glob(".fred-receipt-*"))


def test_fixed_manifest_mismatch_stops_before_stored_read(tmp_path, packet):
    _, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    result = json.loads((directory / "result.json").read_bytes())
    store, manifest = compressed(raw, result)
    key = f"{live_prefix()}/handoff.json"
    expected = digest(store.values[key].body)
    manifest["result"] = {**result, "clock_changed": True}
    store.store_object(key, json.dumps(manifest).encode())
    with pytest.raises(LakePublishError, match="fixed source handoff manifest"):
        load_live(store, tmp_path / "output", expected=expected)
    assert not store.verified_reads


@pytest.mark.parametrize("late", [False, True])
def test_explicit_live_read_keeps_existing_packet_intact(tmp_path, packet, monkeypatch, late):
    _, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    result = json.loads((directory / "result.json").read_bytes())
    store, _ = compressed(raw, result)
    output = tmp_path / "output"
    original_replace = Path.replace

    def retained_packet():
        output.mkdir()
        (output / "result.json").mkdir()
        (output / "receipts.sqlite").write_bytes(b"retained unrelated SQLite")

    def compete(path, target):
        if Path(target) == output:
            retained_packet()
        return original_replace(path, target)

    if late:
        monkeypatch.setattr(Path, "replace", compete)
    else:
        retained_packet()
    with pytest.raises(OSError, match=r"new or empty|Directory not empty|File exists"):
        load_live(store, output)
    assert {path.name for path in output.iterdir()} == {"result.json", "receipts.sqlite"}
    assert (output / "result.json").is_dir()
    assert (output / "receipts.sqlite").read_bytes() == b"retained unrelated SQLite"
    assert len(store.verified_reads) == (1 if late else 0)
    assert not store.put_keys
    assert not list(tmp_path.glob(".fred-receipt-*"))


def test_compressed_restore_rejects_symlink_to_an_empty_destination(tmp_path, packet):
    _, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    result = json.loads((directory / "result.json").read_bytes())
    store, _ = compressed(raw, result)
    destination = tmp_path / "empty"
    destination.mkdir()
    link = tmp_path / "output"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(FileExistsError, match="new or empty packet directory"):
        load_live(store, link)
    assert link.is_symlink()
    assert not list(destination.iterdir())
    assert not store.verified_reads


@pytest.mark.parametrize("failure_point", ["after_payload_put", "after_manifest_put"])
def test_retry_of_uncertain_stage_never_adds_a_second_payload(
    tmp_path, packet, monkeypatch, failure_point
):
    _, directory = packet
    store = VerifiedReads()
    manifest_key = f"{live_prefix()}/handoff.json"
    real_put = store.put_file

    def interrupted(key, *args, **kwargs):
        remote = real_put(key, *args, **kwargs)
        if (failure_point == "after_manifest_put") == (key == manifest_key):
            raise R2Error("simulated lost PUT acknowledgement")
        return remote

    monkeypatch.setattr(store, "put_file", interrupted)
    with pytest.raises(R2Error):
        save_live(store, directory)
    published = dict(store.values)
    monkeypatch.setattr(store, "put_file", real_put)
    puts = len(store.put_keys)
    with pytest.raises(ValueError, match="only its compressed writer"):
        save_handoff(store, STAGE, directory, namespace=FRED_HANDOFF_NAMESPACE)
    assert len(store.put_keys) == puts
    assert store.values == published

    assert save_live(store, directory) == digest(store.values[f"{live_prefix()}/handoff.json"].body)
    assert len(store.values) == 2
    assert len(set(store.put_keys)) == len(store.put_keys) == 2
    assert len(store.verified_reads) == (1 if failure_point == "after_payload_put" else 0)
    [entry] = available_handoffs(store, market=False)
    download_fixed(store, entry, tmp_path / "replayed")
    assert (tmp_path / "replayed" / "receipts.sqlite").read_bytes() == (
        directory / "receipts.sqlite"
    ).read_bytes()


def test_same_codec_conditional_races_accept_only_identical_objects(packet, monkeypatch):
    _, directory = packet
    store = VerifiedReads()
    real_put = store.put_file

    def raced(key, *args, **kwargs):
        assert kwargs["if_none_match"] is True
        real_put(key, *args, **kwargs)
        raise LakeCASConflict("the exact object appeared concurrently")

    monkeypatch.setattr(store, "put_file", raced)
    assert save_live(store, directory) == digest(store.values[f"{live_prefix()}/handoff.json"].body)
    assert len(store.values) == len(store.put_keys) == 2
    old_objects = dict(store.values)
    result = directory / "result.json"
    result.write_text(json.dumps({**json.loads(result.read_bytes()), "changed": True}))
    with pytest.raises(LakePublishError, match="different receipts"):
        save_live(store, directory)
    assert store.values == old_objects
    assert len(store.put_keys) == 2


@pytest.mark.parametrize("reason", ["raw_above_bound", "not_enough_savings"])
def test_live_never_falls_back_to_live_or_second_raw_format(packet, monkeypatch, reason):
    _, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    if reason == "raw_above_bound":
        monkeypatch.setattr(staging, "_MAX_FRED_RECEIPT_BYTES", len(raw) - 1)
    else:
        monkeypatch.setattr(staging, "_FRED_MANIFEST_OVERHEAD_BYTES", len(raw))
    store = VerifiedReads()
    with pytest.raises(ValueError, match=r"exceeds its raw byte bound|does not reduce"):
        save_live(store, directory)
    assert not store.values
    assert not store.put_keys
    assert not store.head_keys


@pytest.mark.parametrize("case", ["wrong_source", "wrong_stage", "not_sqlite"])
def test_live_does_not_publish_wrong_scope_or_invalid_sqlite(packet, case):
    _, directory = packet
    if case == "wrong_source":
        result = directory / "result.json"
        result.write_text(json.dumps({**json.loads(result.read_bytes()), "group": "boj"}))
    if case == "not_sqlite":
        (directory / "receipts.sqlite").write_bytes(b"not SQLite" * 1024)
    store = VerifiedReads()
    stage = "boj-2026-11-22-42-1" if case == "wrong_stage" else STAGE
    with pytest.raises(ValueError, match="requires"):
        save_live(store, directory, stage=stage, validate=lambda _: None)
    assert not store.values
    assert not store.head_keys
    assert not store.get_keys
    assert not store.put_keys


def archived_raw(store, stage, directory, *, prefix=None):
    prefix = stage_prefix(stage) if prefix is None else prefix
    raw = (directory / "receipts.sqlite").read_bytes()
    report = json.loads((directory / "result.json").read_bytes())
    key = f"{prefix}/receipts-{digest(raw)}.sqlite"
    store.store_object(key, raw, content_type="application/vnd.sqlite3")
    store.store_object(
        f"{prefix}/handoff.json",
        json.dumps(
            {
                "result": report,
                "receipt_key": key,
                "receipt_sha256": digest(raw),
                "receipt_bytes": len(raw),
            },
            sort_keys=True,
        ).encode(),
    )


def test_current_cli_stages_only_zstd_and_daily_leaves_old_raw_untouched(
    tmp_path, packet, monkeypatch
):
    canonical, directory = packet
    store = VerifiedReads()
    archived_raw(store, STAGE, directory)
    preserved = dict(store.values)
    assert not available_handoffs(store, market=False)
    assert not available_handoffs(store, market=False, source="economic-fred_csv")
    assert not store.get_keys
    assert not store.head_keys

    monkeypatch.setattr(handoff, "Boto3R2Store", lambda **kwargs: store)
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    assert handoff.main(["stage-fred", "--stage-id", STAGE, "--directory", str(directory)]) == 0
    assert set(store.values) - set(preserved) == {
        f"{live_prefix()}/handoff.json",
        *(
            name
            for name in store.values
            if name.startswith(live_prefix()) and name.endswith(".zst")
        ),
    }
    heads, gets = len(store.head_keys), len(store.get_keys)
    report = integrate_economic(store, tmp_path / "live", canonical, ASOF)
    [outcome] = report["handoffs"]
    assert outcome["entry"]["stage_id"] == STAGE
    assert outcome["entry"]["namespace"] == FRED_HANDOFF_NAMESPACE
    assert outcome["applied"] is True
    assert outcome["failed"] is False
    assert report["l1_sealed"] is True
    assert "economic-fred_csv" in report["inputs_updated"]
    assert all(store.values[k] == v for k, v in preserved.items())
    assert not set(preserved).intersection(store.head_keys[heads:] + store.get_keys[gets:])
    assert f"{live_prefix()}/integrated.json" in store.values
    assert f"{stage_prefix(STAGE)}/integrated.json" not in store.values
    archived_entry = {k: v for k, v in outcome["entry"].items() if k != "namespace"}
    for source in ("economic-fred_csv", "economic-boj"):
        with pytest.raises(ValueError, match="only the live FRED inbox"):
            seal_integrated(
                store,
                {**archived_entry, "source": source},
                generations={},
                recovery=False,
            )
    assert f"{stage_prefix(STAGE)}/integrated.json" not in store.values
    with pytest.raises(ValueError, match="raw FRED acquisition handoffs are archived"):
        load_handoff(store, STAGE, tmp_path / "old")


def test_raw_fred_in_the_active_inbox_is_rejected_without_terminal(tmp_path, packet):
    canonical, directory = packet
    store = VerifiedReads()
    archived_raw(store, STAGE, directory, prefix=live_prefix())
    frozen = dict(store.values)
    with pytest.raises(ValueError, match="accepts only its compressed receipt format"):
        load_live(store, tmp_path / "unexpected")
    [entry] = available_handoffs(store, market=False)
    assert entry["invalid"] == "ValueError"
    integrated = integrate_economic(store, tmp_path / "live", canonical, ASOF)
    [outcome] = integrated["handoffs"]
    assert outcome["failed"]
    assert not outcome["applied"]
    assert not any(
        k.startswith(live_prefix()) and k.endswith("/integrated.json") for k in store.values
    )
    assert all(store.values[k] == v for k, v in frozen.items())
    assert not store.verified_reads


def test_no_raw_switch_or_archived_authority_in_current_cli(packet, monkeypatch):
    _, directory = packet
    store = VerifiedReads()

    def never_store(**_kwargs):
        pytest.fail("pre-cutoff entrypoint must not construct an object store")

    monkeypatch.setattr(handoff, "Boto3R2Store", never_store)
    assert handoff.main(["stage", "--stage-id", STAGE, "--directory", str(directory)]) == 1
    with pytest.raises(SystemExit, match="2"):
        handoff.main(
            ["stage", "--stage-id", STAGE, "--directory", str(directory), "--compress-fred-receipt"]
        )
    with pytest.raises(ValueError, match="raw FRED acquisition handoffs are archived"):
        save_handoff(store, STAGE, directory)
    assert not store.values


def test_live_rejects_wal_receipt_before_store_writes(tmp_path, packet):
    _, directory = packet
    db = directory / "receipts.sqlite"
    wal = db.with_suffix(".sqlite-wal")
    store = VerifiedReads()
    with closing(sqlite3.connect(db)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        raw = db.read_bytes()
        store_snapshot(
            writer,
            source_id=SOURCE,
            observed_at=date(2026, 11, 2),
            fetched_at=CLOCK + timedelta(hours=1),
            records=[{"value": "committed in WAL"}],
        )
        writer.commit()
        assert wal.stat().st_size > 0
        assert db.read_bytes() == raw
        assert (
            writer.execute(
                "SELECT count(*) FROM economic_source_records WHERE observed_at='2026-11-02'"
            ).fetchone()[0]
            == 1
        )
        with pytest.raises(ValueError, match="without sidecars"):
            save_live(store, directory)
    assert not store.values
    assert not store.put_keys

    assert not wal.exists()
    fixed = save_live(store, directory)
    restored = tmp_path / "restored"
    load_live(store, restored, expected=fixed)
    assert (restored / "receipts.sqlite").read_bytes() == db.read_bytes()
    with closing(sqlite3.connect(restored / "receipts.sqlite")) as reader:
        assert (
            reader.execute(
                "SELECT count(*) FROM economic_source_records WHERE observed_at='2026-11-02'"
            ).fetchone()[0]
            == 1
        )


def test_live_rejects_packet_replacement_after_validation(packet):
    _, directory = packet
    store = VerifiedReads()

    def validate_then_replace(path):
        validate_closed(path)
        report = directory / "result.json"
        replacement = report.with_suffix(".new")
        replacement.write_text(report.read_text(), encoding="utf-8")
        replacement.replace(report)

    with pytest.raises(ValueError, match="changed during validation"):
        save_live(store, directory, validate=validate_then_replace)
    assert not store.values
    assert not store.put_keys


@pytest.mark.parametrize("fault", ["already_present", "appears_during_cas"])
def test_pending_compressed_candidate_must_prove_body_bytes_before_manifest(
    packet, monkeypatch, fault
):
    _, directory = packet
    original = VerifiedReads()
    save_live(original, directory)
    prefix = live_prefix()
    manifest = json.loads(original.values[f"{prefix}/handoff.json"].body)
    key = manifest["receipt_key"]
    payload = original.values[key].body
    wrong = bytes([payload[0] ^ 0xFF]) + payload[1:]
    store = VerifiedReads()
    if fault == "already_present":
        store.store_object(
            key, wrong, sha256=manifest["stored_sha256"], content_type="application/zstd"
        )
    else:
        real_put = store.put_file

        def race(requested_key, *args, **kwargs):
            if requested_key != key:
                return real_put(requested_key, *args, **kwargs)
            store.store_object(
                key, wrong, sha256=manifest["stored_sha256"], content_type="application/zstd"
            )
            raise LakeCASConflict("another packet already appeared")

        monkeypatch.setattr(store, "put_file", race)
    with pytest.raises(LakePublishError, match="fixed receipt read failed"):
        save_live(store, directory)
    assert set(store.values) == {key}
    assert store.values[key].body == wrong
    assert store.verified_reads == [(key, manifest["stored_sha256"], manifest["stored_bytes"])]
    assert not available_handoffs(store, market=False)


def test_active_fred_terminal_requires_source_and_clock_and_never_uses_old_alias(packet):
    _, directory = packet
    store = VerifiedReads()
    fixed = save_live(store, directory)
    key = f"{live_prefix()}/integrated.json"
    store.store_object(key, json.dumps({"status": "integrated", "manifest_sha256": fixed}).encode())
    [entry] = available_handoffs(store, market=False, source="economic-fred_csv")
    assert entry["invalid"] == "ValueError"
    assert not store.verified_reads


def test_nonstandard_archive_id_does_not_read_raw_payload_or_terminal(packet):
    _, directory = packet
    store = VerifiedReads()
    stage = "42-1-fred_csv"
    archived_raw(store, stage, directory)
    key = f"{stage_prefix(stage)}/handoff.json"
    assert not available_handoffs(store, market=False, source="economic-fred_csv")
    assert not store.get_keys
    assert not available_handoffs(store, market=False)
    assert store.get_keys == [key]
    assert f"{stage_prefix(stage)}/integrated.json" not in store.head_keys
