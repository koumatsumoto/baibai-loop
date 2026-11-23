"""FRED storage format acceptance using only local packets and a simulated store."""

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
from tests.helpers.r2_store import MemoryR2Store

import baibai_jobs.acquisition.economic_staging as staging
import baibai_jobs.integration.economic_handoff as handoff
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, R2Error
from baibai_jobs.acquisition.economic_staging import load_handoff, save_handoff, stage_prefix
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


class VerifiedReads(MemoryR2Store):
    def __init__(self):
        super().__init__()
        self.verified_reads = []

    def get_verified_object(self, key, *, sha256, max_bytes):
        self.verified_reads.append((key, sha256, max_bytes))
        response = self.get_bytes_with_metadata(key)
        if response is None:
            return None
        payload, info = response
        if (
            not 0 < info.size <= max_bytes
            or info.metadata.get("sha256") != sha256
            or digest(payload) != sha256
        ):
            raise LakePublishError("fixture fixed read failed")
        return payload, info


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


def compressed(raw, result, *, stage=STAGE, wire=None, namespace=None, market=False, **overrides):
    wire = zstd.compress(raw, level=1) if wire is None else wire
    prefix = stage_prefix(stage, market=market, namespace=namespace)
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
def test_legacy_writer_and_compressed_reader_keep_exact_raw_replay_and_fixed_marker(
    tmp_path, packet, monkeypatch, existing_empty
):
    # Exercise buffered decoder output and multiple input chunks on a small SQLite.
    monkeypatch.setattr(staging, "_FRED_DECODE_CHUNK", 127)
    original, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    result = json.loads((directory / "result.json").read_bytes())
    baseline = MemoryR2Store()
    assert save_handoff(baseline, STAGE, directory) == STAGE
    prefix = stage_prefix(STAGE)
    key = f"{prefix}/handoff.json"
    assert "receipt_format" not in json.loads(baseline.values[key].body)
    assert set(baseline.values) == {key, f"{prefix}/receipts-{digest(raw)}.sqlite"}
    candidate = VerifiedReads()
    assert save_handoff(candidate, STAGE, directory, compress_fred=True) == STAGE
    manifest = json.loads(candidate.values[key].body)
    assert manifest["receipt_format"] == FORMAT
    assert manifest["receipt_sha256"] == digest(raw)
    assert manifest["receipt_bytes"] == len(raw)
    assert manifest["stored_bytes"] + 512 < len(raw)
    assert set(candidate.values) == {key, manifest["receipt_key"]}

    replays = []
    for name, store in (("raw", baseline), ("compressed", candidate)):
        restored = tmp_path / name
        if existing_empty:
            restored.mkdir()
        frozen_manifest = store.values[key].body
        [entry] = available_handoffs(store, market=False)
        puts = list(store.put_keys)
        heads = len(store.head_keys)
        download_fixed(store, entry, restored)
        assert store.put_keys == puts
        assert {path.name for path in restored.iterdir()} == {"result.json", "receipts.sqlite"}
        assert (restored / "receipts.sqlite").read_bytes() == raw
        assert json.loads((restored / "result.json").read_bytes()) == result
        if name == "compressed":
            assert candidate.verified_reads == [
                (manifest["receipt_key"], manifest["stored_sha256"], manifest["stored_bytes"])
            ]
            assert manifest["receipt_key"] not in candidate.head_keys[heads:]
            assert all(path.stat().st_mode & 0o777 == 0o600 for path in restored.iterdir())
            assert restored.stat().st_mode & 0o777 == 0o700
        validate_handoff(restored, "fred_csv", SOURCE)
        canonical = restored / "canonical.sqlite"
        copyfile(original, canonical)
        selected = restored / "selected.sqlite"
        replay = accept_rechecks(restored / "receipts.sqlite", canonical, selected)
        merge_stores(selected, canonical)
        verify_stores(selected, canonical)
        duplicate = accept_rechecks(
            restored / "receipts.sqlite", canonical, restored / "again.sqlite"
        )
        replays.append((replay, duplicate, selected.read_bytes(), canonical.read_bytes()))
        seal_integrated(store, entry, generations={"economic_etag": "fixture"}, recovery=False)
        marker = json.loads(store.values[f"{prefix}/integrated.json"].body)
        assert marker["manifest_sha256"] == entry["manifest_sha256"] == digest(frozen_manifest)
        assert marker["status"] == "integrated"
        assert not available_handoffs(store, market=False)
        assert store.values[key].body == frozen_manifest
    assert replays[0] == replays[1]


@pytest.mark.parametrize(
    ("changes", "scope"),
    [
        ({"result": {"group": "boj"}}, {}),
        ({"planning_key": "not-allowed"}, {}),
        ({}, {"namespace": "market-sources"}),
        ({}, {"namespace": "article-sources", "market": True}),
        ({}, {"namespace": "source-inputs", "input_group": "economic-fred_csv"}),
        ({}, {"input_group": "economic-fred_csv"}),
        ({"receipt_bytes": 128 * 1024**2 + 1}, {}),
        ({"stored_bytes": True}, {}),
        ({"stored_sha256": "short"}, {}),
        ({"receipt_sha256": "short"}, {}),
        ({"receipt_key": "coordination/economic-sources/other/receipts.sqlite.zst"}, {}),
        ({"receipt_format": "fred-acquisition-receipts-zstd-v2"}, {}),
    ],
)
def test_fred_transport_does_not_read_payload_for_invalid_descriptor_or_scope(
    tmp_path, packet, changes, scope
):
    _, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    result = changes.get("result") or json.loads((directory / "result.json").read_bytes())
    namespaces = {key: value for key, value in scope.items() if key != "input_group"}
    store, _ = compressed(
        raw,
        result,
        **namespaces,
        **{key: value for key, value in changes.items() if key != "result"},
    )
    output = tmp_path / "output"
    with pytest.raises((LakePublishError, ValueError)):
        load_handoff(store, STAGE, output, **scope)
    assert not store.verified_reads
    assert not output.exists()


@pytest.mark.parametrize("stage", ["boj-2026-11-22-42-1", "fred_csv-"])
def test_fred_codec_cannot_be_relabelled_under_other_stages(tmp_path, packet, stage):
    _, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    result = json.loads((directory / "result.json").read_bytes())
    store, _ = compressed(raw, result, stage=stage)
    with pytest.raises(ValueError, match="requires its economic acquisition stage"):
        load_handoff(store, stage, tmp_path / "output")
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
    fallback = f"{stage_prefix(STAGE)}/receipts-{manifest['receipt_sha256']}.sqlite"
    store.store_object(fallback, raw)
    output = tmp_path / "output"
    with pytest.raises(LakePublishError):
        load_handoff(store, STAGE, output)
    assert fallback not in store.get_keys
    assert fallback not in store.head_keys
    assert not output.exists()
    assert not list(tmp_path.glob(".fred-receipt-*"))


def test_fixed_manifest_mismatch_stops_before_stored_read(tmp_path, packet):
    _, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    result = json.loads((directory / "result.json").read_bytes())
    store, manifest = compressed(raw, result)
    [entry] = available_handoffs(store, market=False)
    manifest["result"] = {**result, "clock_changed": True}
    store.store_object(f"{stage_prefix(STAGE)}/handoff.json", json.dumps(manifest).encode())
    with pytest.raises(LakePublishError, match="fixed source handoff manifest"):
        download_fixed(store, entry, tmp_path / "output")
    assert not store.verified_reads


@pytest.mark.parametrize("late", [False, True])
def test_download_cli_keeps_existing_packet_intact_and_returns_failure(
    tmp_path, packet, monkeypatch, capsys, late
):
    _, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    result = json.loads((directory / "result.json").read_bytes())
    store, _ = compressed(raw, result)
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    monkeypatch.setattr(handoff, "Boto3R2Store", lambda **kwargs: store)
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
    assert handoff.main(["download", "--stage-id", STAGE, "--directory", str(output)]) == 1
    assert capsys.readouterr().err.startswith("economic handoff failed: ")
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
        load_handoff(store, STAGE, link)
    assert link.is_symlink()
    assert not list(destination.iterdir())
    assert not store.verified_reads


@pytest.mark.parametrize("failure_point", ["after_payload_put", "after_manifest_put"])
def test_retry_of_uncertain_stage_never_adds_a_second_payload(
    tmp_path, packet, monkeypatch, failure_point
):
    _, directory = packet
    store = VerifiedReads()
    manifest_key = f"{stage_prefix(STAGE)}/handoff.json"
    real_put = store.put_file

    def interrupted(key, *args, **kwargs):
        remote = real_put(key, *args, **kwargs)
        if (failure_point == "after_manifest_put") == (key == manifest_key):
            raise R2Error("simulated lost PUT acknowledgement")
        return remote

    monkeypatch.setattr(store, "put_file", interrupted)
    with pytest.raises(R2Error):
        save_handoff(store, STAGE, directory, compress_fred=True)
    published = dict(store.values)
    monkeypatch.setattr(store, "put_file", real_put)
    puts = len(store.put_keys)
    with pytest.raises(LakePublishError, match="different"):
        save_handoff(store, STAGE, directory)
    assert len(store.put_keys) == puts
    assert store.values == published

    assert save_handoff(store, STAGE, directory, compress_fred=True) == STAGE
    assert len(store.values) == 2
    assert len(set(store.put_keys)) == len(store.put_keys) == 2
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
    assert save_handoff(store, STAGE, directory, compress_fred=True) == STAGE
    assert len(store.values) == len(store.put_keys) == 2
    old_objects = dict(store.values)
    result = directory / "result.json"
    result.write_text(json.dumps({**json.loads(result.read_bytes()), "changed": True}))
    with pytest.raises(LakePublishError, match="different receipts"):
        save_handoff(store, STAGE, directory, compress_fred=True)
    assert store.values == old_objects
    assert len(store.put_keys) == 2


@pytest.mark.parametrize("reason", ["raw_above_bound", "not_enough_savings"])
def test_only_one_raw_object_when_compression_is_not_eligible(packet, monkeypatch, reason):
    _, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    if reason == "raw_above_bound":
        monkeypatch.setattr(staging, "_MAX_FRED_RECEIPT_BYTES", len(raw) - 1)
    else:
        monkeypatch.setattr(staging, "_FRED_MANIFEST_OVERHEAD_BYTES", len(raw))
    raw_store, opted_in = VerifiedReads(), VerifiedReads()
    save_handoff(raw_store, STAGE, directory)
    save_handoff(opted_in, STAGE, directory, compress_fred=True)
    assert opted_in.values == raw_store.values
    assert len(opted_in.values) == len(opted_in.put_keys) == 2


@pytest.mark.parametrize(
    ("case", "scope"),
    [
        ("wrong_source", {}),
        ("wrong_stage", {}),
        ("market", {"market": True}),
        ("source_input", {"namespace": "source-inputs", "input_group": "economic-fred_csv"}),
        ("not_sqlite", {}),
    ],
)
def test_writer_does_not_publish_wrong_scope_or_invalid_sqlite(packet, case, scope):
    _, directory = packet
    if case == "wrong_source":
        result = directory / "result.json"
        result.write_text(json.dumps({**json.loads(result.read_bytes()), "group": "boj"}))
    if case == "not_sqlite":
        (directory / "receipts.sqlite").write_bytes(b"not SQLite" * 1024)
    store = VerifiedReads()
    stage = "boj-2026-11-22-42-1" if case == "wrong_stage" else STAGE
    with pytest.raises(ValueError, match="compressed FRED requires"):
        save_handoff(store, stage, directory, compress_fred=True, **scope)
    assert not store.values
    assert not store.head_keys
    assert not store.get_keys
    assert not store.put_keys


def test_opt_in_stage_cli_reaches_actual_economic_readback_seal_and_scoped_input(
    tmp_path, packet, monkeypatch
):
    canonical, directory = packet
    raw = (directory / "receipts.sqlite").read_bytes()
    paths = []
    expected = None
    for compression in (False, True):
        store = VerifiedReads()
        monkeypatch.setattr(store, "close", lambda: None, raising=False)
        monkeypatch.setattr(handoff, "Boto3R2Store", lambda store=store, **kwargs: store)
        args = ["stage", "--stage-id", STAGE, "--directory", str(directory)]
        if compression:
            args.append("--compress-fred-receipt")
        assert handoff.main(args) == 0
        prefix = stage_prefix(STAGE)
        manifest_key = f"{prefix}/handoff.json"
        manifest_body = store.values[manifest_key].body
        manifest = json.loads(manifest_body)
        key = manifest["receipt_key"]
        assert set(store.values) == {manifest_key, key}
        assert key.endswith(".zst" if compression else ".sqlite")
        assert manifest["receipt_sha256"] == digest(raw)
        assert manifest["receipt_bytes"] == len(raw)
        if compression:
            assert manifest["stored_sha256"] == digest(store.values[key].body)
            assert manifest["stored_bytes"] == len(store.values[key].body)
            assert store.values[key].content_type == "application/zstd"

        work = tmp_path / str(compression)
        output = tmp_path / f"canonical-{compression}.sqlite"
        copyfile(canonical, output)
        report = integrate_economic(store, work, output, ASOF)
        assert report["l1_published"] is report["l1_sealed"] is True
        assert "economic-fred_csv" in report["inputs_updated"]
        [outcome] = report["handoffs"]
        assert outcome["applied"] is True
        assert outcome["failed"] is outcome["recovery"] is False
        marker = json.loads(store.values[f"{prefix}/integrated.json"].body)
        assert marker["status"] == "integrated"
        assert (
            marker["manifest_sha256"]
            == outcome["entry"]["manifest_sha256"]
            == digest(manifest_body)
        )
        assert marker["canonical_generations"] == report["canonical_generations"]
        assert not available_handoffs(store, market=False)
        inputs = sorted(key for key, _, _ in store.verified_reads)
        if compression:
            assert key in inputs
        assert len([name for name in store.values if name.startswith(prefix + "/receipts-")]) == 1
        original = work.glob("*/receipt-00000/receipts.sqlite")
        assert next(original).read_bytes() == raw
        if expected is None:
            expected = outcome["result"]
        else:
            assert outcome["result"] == expected
        paths.append(output)
    assert paths[0].read_bytes() == paths[1].read_bytes()
