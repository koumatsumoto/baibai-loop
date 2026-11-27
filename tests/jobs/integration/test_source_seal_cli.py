"""Current market seals retain canonical-pair and live lease boundaries."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store, seed_machine_current

from baibai_engine.storage.snapshots.keys import current_release_pointer_key, release_manifest_key
from baibai_engine.storage.snapshots.models import canonical_lake_model_bytes
from baibai_engine.storage.snapshots.release import LakeReleasePointer
from baibai_jobs.integration import sources
from baibai_jobs.publication.lease import (
    PUBLICATION_LEASE_KEY,
    _load_handle,
    _save_handle,
    acquire,
    release,
)

DAY = "2026-11-26"

GENERATIONS = {"lake_release_id": "fixed", "lake_manifest_sha256": "a" * 64}


def _lake_pointer():
    return LakeReleasePointer(
        release_id=GENERATIONS["lake_release_id"],
        manifest_key=release_manifest_key(release_id=GENERATIONS["lake_release_id"]),
        manifest_sha256=GENERATIONS["lake_manifest_sha256"],
    )


def _store_and_lease(tmp_path, monkeypatch, *, lease_now=None):
    remote = MemoryR2Store()
    monkeypatch.setattr(remote, "close", lambda: None, raising=False)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: remote)
    seed_machine_current(remote, _lake_pointer())
    # Real JPX dataset readback is covered separately; these cases isolate pair/lease writes.
    monkeypatch.setattr(sources, "verify_jpx_publication", lambda *_, **__: {})
    handle, _ = acquire(remote, "daily", now=lease_now)
    lease_path = tmp_path / "lease.json"
    _save_handle(lease_path, handle)
    remote.put_keys.clear()
    return remote


def test_retired_market_inputs_command_cannot_open_store(tmp_path, monkeypatch, capsys):
    def unexpected_store(**_):
        pytest.fail("a removed planning command cannot open the store")

    monkeypatch.setattr(sources, "Boto3R2Store", unexpected_store)
    path = tmp_path / "uncreated"
    with pytest.raises(SystemExit) as error:
        sources.main(["inputs", "--directory", str(path), "--group", "boj_news"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert not path.exists()


def _seal_cli(tmp_path, lease_path):
    directory = tmp_path / "integration"
    directory.mkdir(exist_ok=True)
    (directory / "applied.json").write_text(
        json.dumps(
            {
                "handoffs": [
                    {
                        "entry": {
                            "market": True,
                            "stage_id": f"jpx-guard-{index}",
                            "source": "jpx-earnings",
                            "manifest_sha256": str(index) * 64,
                            "started_at": f"{DAY}T00:0{index}:00+00:00",
                        },
                        "applied": True,
                        "result": {"retained_bundles": [f"fixture-original-{index}"]},
                        "recovery": False,
                    }
                    for index in range(2)
                ]
            }
        )
    )
    return ["seal", "--directory", str(directory), "--lease-handle", str(lease_path)]


@pytest.mark.parametrize("state", ["missing", "malformed", "expired", "released"])
def test_market_seal_refuses_invalid_lease_before_any_write(tmp_path, monkeypatch, state):
    expired = datetime.now(UTC) - timedelta(days=1) if state == "expired" else None
    remote = _store_and_lease(tmp_path, monkeypatch, lease_now=expired)
    path = tmp_path / "lease.json"
    if state == "missing":
        path.unlink()
    elif state == "malformed":
        path.write_text("{")
    elif state == "released":
        release(remote, _load_handle(path))
    command = _seal_cli(tmp_path, path)
    remote.put_keys.clear()
    assert sources.main(command) == 1
    assert remote.put_keys == []


@pytest.mark.parametrize("changed", ["lake_only", "missing_market", "changed_runs"])
def test_market_seal_refuses_an_unpaired_current_release(tmp_path, monkeypatch, changed):
    remote = _store_and_lease(tmp_path, monkeypatch)
    command = _seal_cli(tmp_path, tmp_path / "lease.json")
    key = current_release_pointer_key()
    paired = LakeReleasePointer.model_validate_json(remote.values[key].body)
    assert paired.machine is not None
    if changed == "lake_only":
        lake = LakeReleasePointer(
            release_id="successor",
            manifest_key=release_manifest_key(release_id="successor"),
            manifest_sha256="b" * 64,
        )
        remote.store_object(key, canonical_lake_model_bytes(lake))
    elif changed == "missing_market":
        remote.values.pop(paired.machine.market.key)
    else:
        remote.store_object(paired.machine.runs.key, b"mismatched runs")
    before = dict(remote.values)
    remote.put_keys.clear()
    assert sources.main(command) == 1
    assert not remote.put_keys
    assert remote.values == before

    seed_machine_current(remote, _lake_pointer())
    remote.put_keys.clear()
    assert sources.main(command) == 0
    assert len(remote.put_keys) == 2
    for marker in remote.put_keys:
        result = json.loads(remote.values[marker].body)
        assert result["status"] == "integrated"
        assert result["canonical_generations"] == GENERATIONS


def test_ordered_market_seals_stop_after_release_and_resume_without_rewriting(
    tmp_path, monkeypatch
):
    remote = _store_and_lease(tmp_path, monkeypatch)
    command = _seal_cli(tmp_path, tmp_path / "lease.json")
    handle = _load_handle(tmp_path / "lease.json")
    put = remote.put_file
    released = False

    def after_first_marker(key, *args, **kwargs):
        nonlocal released
        result = put(key, *args, **kwargs)
        if key.endswith("/integrated.json") and not released:
            released = True
            release(remote, handle)
        return result

    monkeypatch.setattr(remote, "put_file", after_first_marker)
    assert sources.main(command) == 1
    markers = [key for key in remote.put_keys if key != PUBLICATION_LEASE_KEY]
    assert markers == ["coordination/market-sources/jpx-guard-0/integrated.json"]
    original = remote.values[markers[0]]
    assert "coordination/market-sources/jpx-guard-1/integrated.json" not in remote.values

    next_handle, _ = acquire(remote, "operator")
    path = tmp_path / "next-lease.json"
    _save_handle(path, next_handle)
    command[command.index("--lease-handle") + 1] = str(path)
    remote.put_keys.clear()
    assert sources.main(command) == 0
    assert remote.put_keys == ["coordination/market-sources/jpx-guard-1/integrated.json"]
    assert remote.values[markers[0]] == original
