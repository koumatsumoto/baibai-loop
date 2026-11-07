"""運用例そのものをMemoryR2Storeへ実行する。本番やproviderには接続しない。"""

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.tools.test_inspect_economic_confirmation_gaps import (
    AT,
    DAY,
    receive,
    stage,
)

from baibai_jobs.acquisition.economic_staging import stage_prefix
from baibai_jobs.acquisition.receipts import available_handoffs

RECIPE = Path(__file__).resolve().parents[2] / "tools/diagnostics/economic_confirmation_readonly.md"


def run_recipe(index, arguments, monkeypatch):
    programs = re.findall(r"<<'PY'\n(.*?)\nPY\n```", RECIPE.read_text(), re.DOTALL)
    assert len(programs) == 3
    monkeypatch.setattr(sys, "argv", ["-", *map(str, arguments)])
    # The executable example is repository-owned, not provider or user content.
    exec(compile(programs[index], str(RECIPE), "exec"), {"__name__": "__main__"})  # nosec B102


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    store = MemoryR2Store()
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    monkeypatch.setattr("baibai_engine.storage.objects.r2.Boto3R2Store", lambda **kwargs: store)
    canonical = tmp_path / "canonical.sqlite"
    receive(canonical, "A", 0)
    store.store_object("indicators.sqlite", canonical.read_bytes())
    packet = stage(tmp_path, [("A", 3, {})], identity="nikkei_indexes-synthetic")
    prefix = stage_prefix(packet.stage_id)
    manifest = json.loads((packet.directory / "handoff.json").read_text())
    store.store_object(manifest["receipt_key"], (packet.directory / "receipts.sqlite").read_bytes())
    for name in ("handoff.json", "integrated.json"):
        store.store_object(prefix + "/" + name, (packet.directory / name).read_bytes())
    store.store_object("application.sqlite", b"outside-the-allowed-inputs")
    return store, packet


def metadata_file(tmp_path, packet, monkeypatch, capsys):
    run_recipe(0, [packet.stage_id], monkeypatch)
    text = capsys.readouterr().out
    metadata = tmp_path / "metadata.json"
    metadata.write_text(text)
    value = json.loads(text)
    assert value["markers"][packet.stage_id]["status"] == "integrated"
    assert value["targets"][0]["manifest_sha256"] == packet.manifest_sha256
    return metadata


def test_documented_copy_commands_reach_existing_cli_without_remote_changes(
    tmp_path, prepared, monkeypatch, capsys
):
    store, packet = prepared
    assert available_handoffs(store, source="economic-nikkei_indexes", market=False) == []
    selected = available_handoffs(
        store, source="economic-nikkei_indexes", market=False, include_integrated=True
    )
    assert [entry["stage_id"] for entry in selected] == [packet.stage_id]
    before = {key: value.body for key, value in store.values.items()}
    metadata = metadata_file(tmp_path, packet, monkeypatch, capsys)
    output = tmp_path / "private-copy"
    run_recipe(1, [output, metadata], monkeypatch)
    assert json.loads(capsys.readouterr().out)["status"] == "copied_read_only"
    for name in ("receipts.sqlite", "handoff.json", "integrated.json"):
        assert (output / packet.stage_id / name).read_bytes() == (
            packet.directory / name
        ).read_bytes()
    with pytest.raises(SystemExit, match="0"):
        run_recipe(
            2, [output, DAY.isoformat(), AT.isoformat(), "2026-11-06T16:00:00+00:00"], monkeypatch
        )
    report = json.loads((output / "report.json").read_text())
    assert report["counts"] == {"confirmation_gap_candidate": 1}
    assert report["production_impact"] == "unverified"
    assert report["coverage"] == "not_established"
    assert report["recovery"] == "not_performed"
    assert before == {key: value.body for key, value in store.values.items()}
    assert store.put_keys == []
    assert "application.sqlite" not in store.head_keys + store.get_keys
    assert output.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("change", ["manifest", "marker", "canonical", "receipt_after_download"])
def test_changed_input_stops_before_success_without_publishing(
    tmp_path, prepared, monkeypatch, capsys, change
):
    store, packet = prepared
    metadata = metadata_file(tmp_path, packet, monkeypatch, capsys)
    if change in {"canonical", "receipt_after_download"}:
        original = store.download_file

        def changed(key, path, *, expect_bytes=None):
            original(key, path, expect_bytes=expect_bytes)
            if (change == "canonical" and key == "indicators.sqlite") or (
                change == "receipt_after_download" and "/receipts-" in key
            ):
                store.store_object(key, b"changed-during-read")

        monkeypatch.setattr(store, "download_file", changed)
    else:
        key = stage_prefix(packet.stage_id) + (
            "/handoff.json" if change == "manifest" else "/integrated.json"
        )
        value = json.loads(store.get_bytes(key))
        value["synthetic_change"] = True
        store.store_object(key, json.dumps(value).encode())
    output = tmp_path / "failed-copy"
    with pytest.raises(SystemExit, match="1"):
        run_recipe(1, [output, metadata], monkeypatch)
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert not (output / "copy.json").exists()
    assert store.put_keys == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manifest_sha256", "0" * 64),
        ("source", "economic-frb_h15"),
        ("started_at", "2020-01-01T00:00:00+00:00"),
        ("status", "unrecognized"),
    ],
)
def test_metadata_rejects_marker_not_bound_to_original(prepared, monkeypatch, capsys, field, value):
    store, packet = prepared
    key = stage_prefix(packet.stage_id) + "/integrated.json"
    marker = json.loads(store.get_bytes(key))
    marker[field] = value
    store.store_object(key, json.dumps(marker).encode())
    with pytest.raises(SystemExit, match="1"):
        run_recipe(0, [packet.stage_id], monkeypatch)
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert store.put_keys == []


@pytest.mark.parametrize("damage", ["foreign_key", "invalid_digest"])
def test_copy_rejects_receipt_scope_before_any_outside_read(
    tmp_path, prepared, monkeypatch, capsys, damage
):
    store, packet = prepared
    metadata = metadata_file(tmp_path, packet, monkeypatch, capsys)
    key = stage_prefix(packet.stage_id) + "/handoff.json"
    manifest = json.loads(store.get_bytes(key))
    if damage == "foreign_key":
        manifest["receipt_key"] = "application.sqlite"
    else:
        manifest["receipt_sha256"] = "outside/../../application"
        manifest["receipt_key"] = (
            stage_prefix(packet.stage_id) + "/receipts-" + manifest["receipt_sha256"] + ".sqlite"
        )
    body = json.dumps(manifest).encode()
    store.store_object(key, body)
    value = json.loads(metadata.read_text())
    value["targets"][0]["manifest_sha256"] = hashlib.sha256(body).hexdigest()
    metadata.write_text(json.dumps(value))
    with pytest.raises(SystemExit, match="1"):
        run_recipe(1, [tmp_path / "failed-scope", metadata], monkeypatch)
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert "application.sqlite" not in store.head_keys + store.get_keys
    assert not any("/receipts-outside" in key for key in store.head_keys + store.get_keys)
    assert store.put_keys == []
