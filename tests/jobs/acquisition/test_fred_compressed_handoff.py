"""Reader-first transport acceptance; these fixtures do not activate a writer."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from shutil import copyfile

import pytest
from compression import zstd
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
from baibai_engine.storage.objects.r2 import LakePublishError
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
    candidate, manifest = compressed(raw, result)
    assert set(candidate.values) == {key, manifest["receipt_key"]}

    replays = []
    for name, store in (("raw", baseline), ("compressed", candidate)):
        restored = tmp_path / name
        if existing_empty:
            restored.mkdir()
        frozen_manifest = store.values[key].body
        [entry] = available_handoffs(store, market=False)
        puts = list(store.put_keys)
        download_fixed(store, entry, restored)
        assert store.put_keys == puts
        assert {path.name for path in restored.iterdir()} == {"result.json", "receipts.sqlite"}
        assert (restored / "receipts.sqlite").read_bytes() == raw
        assert json.loads((restored / "result.json").read_bytes()) == result
        if name == "compressed":
            assert candidate.verified_reads == [
                (manifest["receipt_key"], manifest["stored_sha256"], manifest["stored_bytes"])
            ]
            assert manifest["receipt_key"] not in candidate.head_keys
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
